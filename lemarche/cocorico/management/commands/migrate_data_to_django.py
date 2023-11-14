import os

import pymysql
from django.contrib.gis.geos import GEOSGeometry
from django.core.management.base import BaseCommand
from django.db import IntegrityError
from django.db.models.fields import BooleanField, DateTimeField
from django.utils import timezone
from django.utils.text import slugify

from lemarche.networks.models import Network
from lemarche.sectors.models import Sector, SectorGroup
from lemarche.siaes import constants as siae_constants
from lemarche.siaes.models import Siae, SiaeClientReference, SiaeImage, SiaeLabelOld, SiaeOffer
from lemarche.users.models import User
from lemarche.utils.data import rename_dict_key, reset_app_sql_sequences


DIRECTORY_EXTRA_KEYS = [
    "latitude",
    "longitude",
    "sector",  # string 'list' with ' - ' seperator. We use instead the 'directory_category' table.
]
USER_EXTRA_KEYS = [
    "username",
    "username_canonical",
    "email_canonical",
    "slug",
    "salt",
    "password",
    "confirmation_token",
    "password_requested_at",
    "roles",
    "person_type",
    "birthday",
    "nationality",
    "country_of_residence",
    "profession",
    "mother_tongue",
    # "phone_prefix", "time_zone", "phone_verified", "email_verified", "id_card_verified",
    # "accept_survey", "accept_rgpd", "offers_for_pro_sector", "quote_promise",
    "iban",
    "bic",
    "bank_owner_name",
    "bank_owner_address",
    "annual_income",
    "nb_bookings_offerer",
    "nb_bookings_asker",
    "fee_as_asker",
    "fee_as_offerer",
    "average_rating_as_asker",
    "average_rating_as_offerer",
    "answer_delay",
    "nb_quotes_offerer",
    "nb_quotes_asker",
    "company_addr_string",
]

DIRECTORY_BOOLEAN_FIELDS = [field.name for field in Siae._meta.fields if type(field) is BooleanField]
USER_BOOLEAN_FIELDS = [field.name for field in User._meta.fields if type(field) is BooleanField]

DIRECTORY_DATE_FIELDS = [field.name for field in Siae._meta.fields if type(field) is DateTimeField]
NETWORK_DATE_FIELDS = [field.name for field in Network._meta.fields if type(field) is DateTimeField]
SECTOR_DATE_FIELDS = [field.name for field in Sector._meta.fields if type(field) is DateTimeField]
USER_DATE_FIELDS = [field.name for field in User._meta.fields if type(field) is DateTimeField]


def integer_to_boolean(elem):
    boolean_keys = list(set(DIRECTORY_BOOLEAN_FIELDS + USER_BOOLEAN_FIELDS))
    for key in boolean_keys:
        if key in elem:
            if elem[key] in [1, "1"]:
                elem[key] = True
            elif elem[key] in [0, "0"]:
                elem[key] = False
            elif elem[key] in [None]:
                elem[key] = None  # will use field default
            else:
                elem[key] = False


def cleanup_date_field_names(elem):
    if "createdAt" in elem:
        if elem["createdAt"]:
            elem["created_at"] = elem["createdAt"]
        elem.pop("createdAt")
    if "updatedAt" in elem:
        if elem["updatedAt"]:
            elem["updated_at"] = elem["updatedAt"]
        elem.pop("updatedAt")


def make_aware_dates(elem):
    date_keys = list(set(DIRECTORY_DATE_FIELDS + NETWORK_DATE_FIELDS + SECTOR_DATE_FIELDS + USER_DATE_FIELDS))
    for key in date_keys:
        if key in elem:
            if elem[key]:
                elem[key] = timezone.make_aware(elem[key])


def map_siae_nature(input_value):
    if input_value:
        nature_mapping = {
            "siege": Siae.NATURE_HEAD_OFFICE,
            "antenne": Siae.NATURE_ANTENNA,
            "n/a": None,
            None: None,
        }
        return nature_mapping[input_value]
    return None


def map_siae_presta_type(input_value_byte):
    if input_value_byte:
        input_value_string = input_value_byte.decode()
        presta_type_mapping = {
            None: None,
            "0": [],
            "2": [siae_constants.PRESTA_DISP],
            "4": [siae_constants.PRESTA_PREST],
            "6": [siae_constants.PRESTA_DISP, siae_constants.PRESTA_PREST],
            "8": [siae_constants.PRESTA_BUILD],
            "10": [siae_constants.PRESTA_DISP, siae_constants.PRESTA_BUILD],
            "12": [siae_constants.PRESTA_PREST, siae_constants.PRESTA_BUILD],
            "14": [siae_constants.PRESTA_DISP, siae_constants.PRESTA_PREST, siae_constants.PRESTA_BUILD],
        }
        try:
            return presta_type_mapping[input_value_string]
        except:  # noqa
            pass
    return None


def map_geo_range(input_value_integer):
    geo_range_mapping = {
        3: siae_constants.GEO_RANGE_COUNTRY,
        2: siae_constants.GEO_RANGE_REGION,
        1: siae_constants.GEO_RANGE_DEPARTMENT,
        0: siae_constants.GEO_RANGE_CUSTOM,
        None: None,
    }
    try:
        return geo_range_mapping[input_value_integer]
    except:  # noqa
        return None


def map_user_kind(input_value_integer):
    if input_value_integer:
        user_kind_mapping = {
            None: None,
            # 1: User.KIND_PERSO,
            # 2: User.KIND_COMPANY,
            3: User.KIND_BUYER,
            4: User.KIND_SIAE,
            5: User.KIND_ADMIN,
            6: User.KIND_PARTNER,
        }
        try:
            return user_kind_mapping[input_value_integer]
        except:  # noqa
            pass
    return None


class Command(BaseCommand):
    """
    Migrate from Symphony/MariaDB to Django/PostgreSQL

    |---------------------------|---------------------------|
    |directory                  |Siae                       |
    |network                    |Network                    |
    |directory_network          |M2M between Siae & Network |
    |listing_category & listing_category_translation |Sector|
    |directory_listing_category |M2M between Siae & Sector  |
    |directory_label            |SiaeLabel ("Labels & certifications") + OneToMany between Siae & Label|
    |directory_offer            |SiaeOffer ("Prestations proposées") + OneToMany between Siae & Offer|
    |directory_client_image     |SiaeClientReference ("Références clients") + OneToMany between Siae & SiaeClientReference|  # noqa
    |directory_image            |Siae.image_name            |
    |listing & listing_image & listing_translation |SiaeImage|
    |user                       |User                       |
    |directory_user             |M2M between Siae & User    |
    |user_image                 |User.image_name            |

    Usage: poetry run python manage.py migrate_data_to_django
    """

    def handle(self, *args, **options):
        connMy = pymysql.connect(
            host=os.environ.get("MYSQL_ADDON_HOST"),
            port=int(os.environ.get("MYSQL_ADDON_PORT")),
            user=os.environ.get("MYSQL_ADDON_USER"),
            password=os.environ.get("MYSQL_ADDON_PASSWORD"),
            database=os.environ.get("MYSQL_ADDON_DB"),
            cursorclass=pymysql.cursors.DictCursor,
        )

        try:
            with connMy.cursor() as cur:
                # self.migrate_siae(cur)
                # self.migrate_siae_logo(cur)
                # self.migrate_network(cur)
                # self.migrate_siae_network(cur)
                # self.migrate_sector(cur)
                # self.migrate_siae_sector(cur)
                # self.migrate_siae_offer(cur)
                # self.migrate_siae_label(cur)
                # self.migrate_siae_client_reference_logo(cur)
                self.migrate_siae_image(cur)
                # self.migrate_user(cur)
                # self.migrate_user_image(cur)
                # self.migrate_siae_user(cur)
                # self.update_siae_contact()
        except Exception as e:
            # logger.exception(e)
            # print(e)
            print(e)
            connMy.rollback()
        finally:
            connMy.close()

    def migrate_siae(self, cur):
        """
        Migrate Siae data
        """
        print("-" * 80)
        print("Migrating Siae...")

        Siae.objects.all().delete()

        cur.execute("SELECT * FROM directory ORDER BY is_active DESC")
        resp = cur.fetchall()
        # print(len(resp))

        # s = set([elem["is_qpv"] for elem in resp])
        # print(s)

        # elem = cur.fetchone()
        # print(elem)

        for elem in resp:
            # rename fields
            rename_dict_key(elem, "geo_range", "geo_range_custom_distance")
            rename_dict_key(elem, "pol_range", "geo_range")
            rename_dict_key(elem, "c4_id", "c4_id_old")
            rename_dict_key(elem, "c1_source", "source")  # changed after the migration

            # cleanup fields
            cleanup_date_field_names(elem)
            make_aware_dates(elem)
            integer_to_boolean(elem)

            # cleanup nature
            if "nature" in elem:
                elem["nature"] = map_siae_nature(elem["nature"])

            # cleanup presta_type
            if "presta_type" in elem:
                elem["presta_type"] = map_siae_presta_type(elem["presta_type"])

            # cleanup geo_range
            if "geo_range" in elem:
                elem["geo_range"] = map_geo_range(elem["geo_range"])

            # create coords from latitude & longitude
            if "latitude" in elem and "longitude" in elem:
                if elem["latitude"] and elem["longitude"]:
                    coords = {"type": "Point", "coordinates": [float(elem["longitude"]), float(elem["latitude"])]}
                    elem["coords"] = GEOSGeometry(f"{coords}")  # Feed `GEOSGeometry` with GeoJSON.

            # remove useless keys
            [elem.pop(key) for key in DIRECTORY_EXTRA_KEYS]

            # create object
            try:
                Siae.objects.create(**elem)
            except Exception as e:
                print(e)

        print(f"Created {Siae.objects.count()} siaes !")

    def migrate_siae_logo(self, cur):
        """
        Migrate Siae.image_name data.
        We only take the first one (position == 1)

        Notes:
        - elem exemple: {'id': 1681 'directory_id': 2131, 'name': '29df2732c5b804db41b2cd6149fb46e9ba44ce3f.gif', 'position': 1}  # noqa
        """
        print("-" * 80)
        print("Migrating Siae image names...")

        cur.execute("SELECT * FROM directory_image")
        resp = cur.fetchall()

        for elem in resp:
            if elem["position"] == 1:
                siae = Siae.objects.get(pk=elem["directory_id"])
                siae.image_name = elem["name"]
                siae.save()

        print(f"Added {Siae.objects.exclude(image_name__isnull=True).count()} siae images !")

    def migrate_network(self, cur):
        """
        Migrate Network data

        Notes:
        - fields 'accronym' and 'siret' are always empty
        """
        print("-" * 80)
        print("Migrating Network...")

        Network.objects.all().delete()

        cur.execute("SELECT * FROM network")
        resp = cur.fetchall()

        for elem in resp:
            # cleanup dates
            cleanup_date_field_names(elem)
            make_aware_dates(elem)

            # remove useless keys
            [elem.pop(key) for key in ["accronym", "siret"]]

            # add new keys
            elem["slug"] = slugify(elem["name"])

            # create object
            Network.objects.create(**elem)

        print(f"Created {Network.objects.count()} siae networks !")

    def migrate_siae_network(self, cur):
        """
        Migrate M2M data between Siae & Network

        Notes:
        - elem exemple: {'directory_id': 270, 'network_id': 8}
        """
        print("-" * 80)
        print("Migrating M2M between Siae & Network...")

        Siae.networks.through.objects.all().delete()

        cur.execute("SELECT * FROM directory_network")
        resp = cur.fetchall()

        for elem in resp:
            siae = Siae.objects.get(pk=elem["directory_id"])
            siae.networks.add(elem["network_id"])

        print(f"Created {Siae.networks.through.objects.count()} M2M objects !")

    def migrate_sector(self, cur):
        """
        Migrate Sector & SectorGroup data

        Notes:
        - the current implementation in Symphony is overly complex
        - we simplify it with a simple parent/child hierarchy
        """
        print("-" * 80)
        print("Migrating Sector & SectorGroup...")

        Sector.objects.all().delete()
        SectorGroup.objects.all().delete()
        reset_app_sql_sequences("sectors")

        cur.execute("SELECT * FROM listing_category")
        resp = cur.fetchall()

        # first we recreate the hierarchy Sector Group > Sector Children
        sector_group_list = []
        for elem in resp:
            if not elem["parent_id"]:
                # this is a group elem, create it if it doesn't exist yet
                sector_group_index = next(
                    (index for (index, s) in enumerate(sector_group_list) if s["id"] == elem["id"]), None
                )
                if sector_group_index is None:
                    sector_group_list.append({"id": elem["id"], "children": []})
            else:
                # this is a child elem
                sector_group_index = next(
                    (index for (index, s) in enumerate(sector_group_list) if s["id"] == elem["parent_id"]), None
                )
                if sector_group_index is None:
                    sector_group_list.append({"id": elem["parent_id"], "children": []})
                    sector_group_index = len(sector_group_list) - 1
                sector_group_list[sector_group_index]["children"].append(elem["id"])

        # print(sector_group_list)

        cur.execute("SELECT * FROM listing_category_translation")
        resp = cur.fetchall()

        # then we loop on the hierarchy to create the SectorGroup & Sector objects
        for sector_group_dict in sector_group_list:
            elem_data = next(
                s
                for (index, s) in enumerate(resp)
                if ((s["translatable_id"] == sector_group_dict["id"]) and (s["locale"] == "fr"))
            )
            sector_group = SectorGroup.objects.create(
                pk=sector_group_dict["id"], name=elem_data["name"], slug=elem_data["slug"]
            )
            for sector_id in sector_group_dict["children"]:
                elem_data = next(
                    s
                    for (index, s) in enumerate(resp)
                    if ((s["translatable_id"] == sector_id) and (s["locale"] == "fr"))
                )
                try:
                    Sector.objects.create(
                        pk=sector_id, name=elem_data["name"], slug=elem_data["slug"], group=sector_group
                    )
                except IntegrityError:  # sometimes the slugs are duplicated (e.g. "autre")
                    slug_fix = f"{elem_data['slug']}-{sector_group_dict['id']}"
                    Sector.objects.create(pk=sector_id, name=elem_data["name"], slug=slug_fix, group=sector_group)

        print(f"Created {SectorGroup.objects.count()} sector groups !")
        print(f"Created {Sector.objects.count()} sectors !")

    def migrate_siae_sector(self, cur):
        """
        Migrate M2M data between Siae & Sector

        Notes:
        - elem exemple: {'category_id': 270, 'listing_category_id': 8}
        """
        print("-" * 80)
        print("Migrating M2M between Siae & Sector...")

        Siae.sectors.through.objects.all().delete()

        cur.execute("SELECT * FROM directory_listing_category")
        resp = cur.fetchall()

        progress = 0

        # Sometimes Siaes are linked to a SectorGroup instead of a Sector.
        # We ignore these cases
        for elem in resp:
            try:
                siae = Siae.objects.get(pk=elem["directory_id"])
                siae.sectors.add(elem["listing_category_id"])
                progress += 1
                if (progress % 500) == 0:
                    print(f"{progress}...")
            except:  # noqa
                # print(elem)
                pass

        print(f"Created {Siae.sectors.through.objects.count()} M2M objects !")

    def migrate_siae_offer(self, cur):
        """
        Migrate SiaeOffer data
        """
        print("-" * 80)
        print("Migrating SiaeOffer...")

        SiaeOffer.objects.all().delete()

        cur.execute("SELECT * FROM directory_offer")
        resp = cur.fetchall()

        for elem in resp:
            # rename fields
            rename_dict_key(elem, "directory_id", "siae_id")

            # cleanup fields
            cleanup_date_field_names(elem)
            make_aware_dates(elem)

            # remove useless keys
            [elem.pop(key) for key in ["id"]]

            # create object
            SiaeOffer.objects.create(**elem)

        print(f"Created {SiaeOffer.objects.count()} offers !")

    def migrate_siae_label(self, cur):
        """
        Migrate SiaeLabelOld data
        """
        print("-" * 80)
        print("Migrating SiaeLabelOld...")

        SiaeLabelOld.objects.all().delete()

        cur.execute("SELECT * FROM directory_label")
        resp = cur.fetchall()

        for elem in resp:
            # rename fields
            rename_dict_key(elem, "directory_id", "siae_id")

            # cleanup fields
            cleanup_date_field_names(elem)
            make_aware_dates(elem)

            # remove useless keys
            [elem.pop(key) for key in ["id"]]

            # create object
            SiaeLabelOld.objects.create(**elem)

        print(f"Created {SiaeLabelOld.objects.count()} labels !")

    def migrate_siae_client_reference_logo(self, cur):
        """
        Migrate SiaeClientReference data
        """
        print("-" * 80)
        print("Migrating SiaeClientReference...")

        SiaeClientReference.objects.all().delete()

        cur.execute("SELECT * FROM directory_client_image")
        resp = cur.fetchall()

        for elem in resp:
            # cleanup dates
            cleanup_date_field_names(elem)
            make_aware_dates(elem)

            # rename fields
            rename_dict_key(elem, "name", "image_name")
            rename_dict_key(elem, "description", "name")
            rename_dict_key(elem, "position", "order")
            rename_dict_key(elem, "directory_id", "siae_id")

            # remove useless keys
            [elem.pop(key) for key in ["id"]]

            # create object
            SiaeClientReference.objects.create(**elem)

        print(f"Created {SiaeClientReference.objects.count()} client references !")

    def migrate_siae_image(self, cur):  # noqa C901
        """
        Migrate SiaeImage data
        - first get list from 'listing'
        - enrich with 'listing_translation'
        - finally get all the images from 'listing_image'

        User -- Listing(s) -- Image(s)
        """
        print("-" * 80)
        print("Migrating SiaeImage...")

        SiaeImage.objects.all().delete()

        siae_listing_list = list()

        cur.execute("SELECT * FROM listing")
        resp = cur.fetchall()

        print(f"Found {len(resp)} Siae listings...")

        for elem in resp:
            # cleanup dates
            cleanup_date_field_names(elem)
            make_aware_dates(elem)

            # rename fields
            rename_dict_key(elem, "id", "listing_id")

            # remove useless keys
            elem_thin = {key: elem[key] for key in ["listing_id", "user_id", "created_at", "updated_at"]}
            elem_thin["images"] = list()

            siae_listing_list.append(elem_thin)

        cur.execute("SELECT * FROM listing_translation")
        resp = cur.fetchall()

        for elem in resp:
            # rename fields
            rename_dict_key(elem, "title", "name")

            # remove useless keys
            elem_thin = {key: elem[key] for key in ["translatable_id", "name", "description"]}  # "rules"

            # find corresponding siae_listing item, and enrich it
            siae_listing_index = next(
                (
                    index
                    for (index, si) in enumerate(siae_listing_list)
                    if si["listing_id"] == elem_thin["translatable_id"]
                ),
                None,
            )
            if siae_listing_index:
                siae_listing_list[siae_listing_index] |= elem_thin

        cur.execute("SELECT * FROM listing_image")
        resp = cur.fetchall()

        print(f"Found {len(resp)} Siae images...")

        for elem in resp:
            # rename fields
            rename_dict_key(elem, "name", "image_name")

            # remove useless keys
            elem_thin = {key: elem[key] for key in ["listing_id", "image_name", "position"]}

            # find corresponding siae_listing item, and enrich it
            siae_listing_index = next(
                (index for (index, si) in enumerate(siae_listing_list) if si["listing_id"] == elem_thin["listing_id"]),
                None,
            )
            if siae_listing_index:
                siae_listing_list[siae_listing_index]["images"].append(elem_thin)

        error_count = {"listing_without_image": 0, "user_not_found": 0, "user_no_siae": 0, "user_multiple_siae": 0}
        for siae_listing in siae_listing_list:
            if not len(siae_listing["images"]):
                # print("images missing", siae_listing)
                error_count["listing_without_image"] += 1
            else:
                for index, siae_image in enumerate(siae_listing["images"]):
                    siae_image_dict = siae_listing.copy() | siae_image

                    # rename fields
                    rename_dict_key(siae_image_dict, "listing_id", "c4_listing_id")
                    rename_dict_key(siae_image_dict, "position", "order")

                    users = User.objects.prefetch_related("siaes").filter(c4_id=siae_image_dict["user_id"])
                    if users.count() == 0:
                        # print("missing user...", siae_image_dict)
                        error_count["user_not_found"] += 1
                    else:
                        if users.first().siaes.count() > 1:
                            # print("which siae?", siae_image_dict)
                            error_count["user_multiple_siae"] += 1
                        elif users.first().siaes.count() == 0:
                            # print("no siae...", siae_image_dict)
                            error_count["user_no_siae"] += 1
                        else:  # count == 1
                            # get siae_id
                            siae = users.first().siaes.first()
                            siae_image_dict["siae_id"] = siae.id

                            # we want to group the images by their listing (by updating their order)
                            # listing_count = Siae

                            # remove useless keys
                            [siae_image_dict.pop(key) for key in ["translatable_id", "user_id", "images"]]

                            # create object
                            SiaeImage.objects.create(**siae_image_dict)

        print(f"Created {SiaeImage.objects.count()} siae images !")
        print("Errors", error_count)

    def migrate_user(self, cur):
        """
        Migrate User data
        """
        print("-" * 80)
        print("Migrating User...")

        User.objects.filter(api_key__isnull=True).delete()
        reset_app_sql_sequences("users")

        cur.execute("SELECT * FROM user")
        resp = cur.fetchall()

        for elem in resp:
            # rename fields
            rename_dict_key(elem, "enabled", "is_active")
            rename_dict_key(elem, "id", "c4_id")
            rename_dict_key(elem, "phone_prefix", "c4_phone_prefix")
            rename_dict_key(elem, "time_zone", "c4_time_zone")
            rename_dict_key(elem, "website", "c4_website")
            rename_dict_key(elem, "siret", "c4_siret")
            rename_dict_key(elem, "naf", "c4_naf")
            rename_dict_key(elem, "phone_verified", "c4_phone_verified")
            rename_dict_key(elem, "email_verified", "c4_email_verified")
            rename_dict_key(elem, "id_card_verified", "c4_id_card_verified")
            # rename_dict_key(elem, "accept_survey", "c4_accept_survey")
            # rename_dict_key(elem, "accept_rgpd", "c4_accept_rgpd")
            rename_dict_key(elem, "offers_for_pro_sector", "accept_offers_for_pro_sector")
            rename_dict_key(elem, "quote_promise", "accept_quote_promise")

            # cleanup fields
            cleanup_date_field_names(elem)
            make_aware_dates(elem)
            integer_to_boolean(elem)

            # cleanup person_type
            if "person_type" in elem:
                elem["kind"] = map_user_kind(elem["person_type"])

            # set staff users
            if "roles" in elem:
                if elem["roles"].startswith("a:1:{i:0;s:10"):
                    elem["is_staff"] = True
                if elem["roles"].startswith("a:1:{i:0;s:16"):
                    elem["is_superuser"] = True

            # remove useless keys
            [elem.pop(key) for key in USER_EXTRA_KEYS]

            # create object
            # Note: we ignore users with kind=None
            if elem["kind"]:
                try:
                    User.objects.create(**elem)
                except Exception as e:
                    print("a", e)

        print(f"Created {User.objects.count()} users !")

    def migrate_user_image(self, cur):
        """
        Migrate User.image_name data
        We only take the first one (position == 1)

        Notes:
        - elem exemple: {'id': 3, 'user_id': 1697088192, 'name': 'acbff2af48356f50f777c9ae8435335f6d73782d.gif', 'position': 1}  # noqa
        """
        print("-" * 80)
        print("Migrating User image names...")

        cur.execute("SELECT * FROM user_image")
        resp = cur.fetchall()

        for elem in resp:
            if elem["position"] == 1:
                user = User.objects.get(c4_id=elem["user_id"])
                user.image_name = elem["name"]
                user.save()

        print(f"Added {User.objects.exclude(image_name__isnull=True).count()} user images !")

    def migrate_siae_user(self, cur):
        """
        Migrate M2M data between Siae & User

        Notes:
        - elem exemple: {'directory_id': 270, 'user_id': 471234844}
        """
        print("-" * 80)
        print("Migrating M2M between Siae & User...")

        Siae.users.through.objects.all().delete()

        cur.execute("SELECT * FROM directory_user")
        resp = cur.fetchall()

        for elem in resp:
            try:
                user = User.objects.get(c4_id=elem["user_id"])
                user.siaes.add(elem["directory_id"])
            # Note: some users were ignored because of kind=None. So we ignore the relation as well.
            except:  # noqa
                pass

        print(f"Created {Siae.users.through.objects.count()} M2M objects !")

    def update_siae_contact(self):
        """
        Update SIAE contact fields from user contact info.

        Currently, contact info where taken from the first user.
        We now store these fields directly on the SIAE.
        """
        print("-" * 80)
        print("Updating Siae contact fields...")

        for siae in Siae.objects.has_user():
            # website was already an editable field in C4
            siae.contact_website = siae.website

            first_user = siae.users.order_by("c4_id").first()
            siae.contact_email = first_user.email
            siae.contact_phone = first_user.phone
            siae.contact_first_name = first_user.first_name
            siae.contact_last_name = first_user.last_name

            siae.save()

        # TODO: init contact_website, contact_email & contact_phone for Siae without users as well

        print(f"Updated {Siae.objects.has_user().count()} SIAE !")
