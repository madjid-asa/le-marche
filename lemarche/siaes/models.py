from uuid import uuid4

from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.gis.db import models as gis_models
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector, SearchVectorField, TrigramSimilarity
from django.db import IntegrityError, models, transaction
from django.db.models import BooleanField, Case, CharField, Count, F, IntegerField, PositiveIntegerField, Q, Sum, When
from django.db.models.functions import Greatest, Round
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.functional import cached_property
from django.utils.text import slugify

from lemarche.perimeters.models import Perimeter
from lemarche.siaes import constants as siae_constants
from lemarche.siaes.tasks import set_siae_coords
from lemarche.siaes.validators import validate_naf, validate_post_code, validate_siret
from lemarche.stats.models import Tracker
from lemarche.users.models import User
from lemarche.utils.constants import DEPARTMENTS_PRETTY, REGIONS_PRETTY
from lemarche.utils.data import round_by_base
from lemarche.utils.fields import ChoiceArrayField


def get_region_filter(perimeter):
    return Q(region=perimeter.name)


def get_department_filter(perimeter):
    return Q(department=perimeter.insee_code)


def get_simple_city_filter(perimeter):
    """
    - if the perimeter is a CITY, return all Siae with the city's post_code
    """
    return Q(post_code__in=perimeter.post_codes)


def get_city_filter(perimeter, with_country=False):
    """
    used in Siae geo_range_in_perimeter_list() queryset
    - if the perimeter is a CITY, return all Siae with the city's post_code
    - also return the Siae in the same department (if not GEO_RANGE_CUSTOM)
    """
    filters = get_simple_city_filter(perimeter) | (
        ~Q(geo_range=siae_constants.GEO_RANGE_CUSTOM) & Q(department=perimeter.department_code)
    )
    if perimeter.coords:
        filters |= (
            Q(geo_range=siae_constants.GEO_RANGE_CUSTOM)
            # why distance / 1000 ? because convert from meter to km
            & Q(geo_range_custom_distance__gte=Distance("coords", perimeter.coords) / 1000)
        )
    if with_country:
        filters |= Q(geo_range=siae_constants.GEO_RANGE_COUNTRY)
    return filters


class SiaeGroup(models.Model):
    TRACK_UPDATE_FIELDS = [
        # set last_updated fields
        "siae_count",
        "employees_insertion_count",
        "employees_permanent_count",
        "ca",
    ]

    name = models.CharField(verbose_name="Nom", max_length=255)
    slug = models.SlugField(verbose_name="Slug", max_length=255, unique=True)
    siret = models.CharField(verbose_name="Siret", max_length=14, blank=True, db_index=True)

    contact_first_name = models.CharField(verbose_name="Prénom", max_length=150, blank=True)
    contact_last_name = models.CharField(verbose_name="Nom", max_length=150, blank=True)
    contact_website = models.URLField(
        verbose_name="Site internet", help_text="Doit commencer par http:// ou https://", blank=True
    )
    contact_email = models.EmailField(verbose_name="E-mail", blank=True, db_index=True)
    contact_phone = models.CharField(verbose_name="Téléphone", max_length=150, blank=True)
    contact_social_website = models.URLField(
        verbose_name="Réseau social", help_text="Doit commencer par http:// ou https://", blank=True
    )

    logo_url = models.URLField(verbose_name="Lien vers le logo", max_length=500, blank=True)

    year_constitution = models.PositiveIntegerField(verbose_name="Année de création", blank=True, null=True)
    siae_count = models.PositiveIntegerField(verbose_name="Nombre de structures", blank=True, null=True)
    siae_count_last_updated = models.DateTimeField(
        verbose_name="Date de dernière mise à jour du nombre de structures", blank=True, null=True
    )
    employees_insertion_count = models.PositiveIntegerField(
        verbose_name="Nombre de salariés en insertion", blank=True, null=True
    )
    employees_insertion_count_last_updated = models.DateTimeField(
        verbose_name="Date de dernière mise à jour du nombre de salariés en insertion", blank=True, null=True
    )
    employees_permanent_count = models.PositiveIntegerField(
        verbose_name="Nombre de salariés permanents", blank=True, null=True
    )
    employees_permanent_count_last_updated = models.DateTimeField(
        verbose_name="Date de dernière mise à jour du nombre de salariés permanents", blank=True, null=True
    )
    ca = models.PositiveIntegerField(verbose_name="Chiffre d'affaires", blank=True, null=True)
    ca_last_updated = models.DateTimeField(
        verbose_name="Date de dernière mise à jour du chiffre d'affaires", blank=True, null=True
    )

    sectors = models.ManyToManyField(
        "sectors.Sector", verbose_name="Secteurs d'activité", related_name="siae_groups", blank=True
    )

    created_at = models.DateTimeField("Date de création", default=timezone.now)
    updated_at = models.DateTimeField("Date de modification", auto_now=True)

    class Meta:
        verbose_name = "Groupement"
        verbose_name_plural = "Groupements"

    def __str__(self):
        return self.name

    def __init__(self, *args, **kwargs):
        """
        https://stackoverflow.com/a/23363123
        """
        super(SiaeGroup, self).__init__(*args, **kwargs)
        for field_name in self.TRACK_UPDATE_FIELDS:
            setattr(self, f"__previous_{field_name}", getattr(self, field_name))

    def set_slug(self):
        """
        The slug field should be unique.
        """
        if not self.slug:
            self.slug = slugify(self.name)[:50]

    def set_last_updated_fields(self):
        """
        We track changes on some fields, in order to update their 'last_updated' counterpart.
        Where are the '__previous' fields set? In the __init__ method
        """
        for field_name in self.TRACK_UPDATE_FIELDS:
            previous_field_name = f"__previous_{field_name}"
            if getattr(self, field_name) and getattr(self, field_name) != getattr(self, previous_field_name):
                try:
                    setattr(self, f"{field_name}_last_updated", timezone.now())
                except AttributeError:
                    pass

    def save(self, *args, **kwargs):
        """Generate the slug field before saving."""
        self.set_slug()
        self.set_last_updated_fields()
        super().save(*args, **kwargs)


class SiaeQuerySet(models.QuerySet):
    def is_live(self):
        return self.filter(is_active=True).filter(is_delisted=False)

    def is_not_live(self):
        return self.filter(Q(is_active=False) | Q(is_delisted=True))

    def prefetch_many_to_many(self):
        return self.prefetch_related("sectors", "networks")  # "users", "groups", "labels"

    def prefetch_many_to_one(self):
        return self.prefetch_related("offers", "client_references", "labels_old", "images")

    def search_query_set(self):
        return self.is_live().exclude(kind="OPCS").prefetch_many_to_many()

    def filter_siret_startswith(self, siret):
        return self.filter(siret__startswith=siret)

    def filter_on_siret_or_name_or_brand(self, search_string):
        # Simple method 1: SearchVectors
        #     return self.annotate(
        #         search=SearchVector("name", config="french") + SearchVector("brand", config="french")
        #     ).filter(Q(search=search_string) | Q(siret__startswith=search_string))
        # Simple method 2: TrigramSimilarity
        return self.annotate(
            similarity=Greatest(TrigramSimilarity("name", search_string), TrigramSimilarity("brand", search_string))
        ).filter(Q(similarity__gt=0.2) | Q(siret__startswith=search_string))

    def filter_full_text(self, search_string):
        search_vector = (
            SearchVector("name")
            + SearchVector("brand")
            + SearchVector("description", config="french")
            + SearchVector("offers__name", config="french")
            + SearchVector("labels__name", config="french")
        )
        search_query = SearchQuery(search_string, config="french")

        return self.annotate(rank=SearchRank(search_vector, search_query)).filter(rank__gte=0.01)

    def filter_full_text_on_search_vector_field(self, search_string):
        # SearchQuery uses 'AND' by default. Change to 'OR'.
        search_string_list = search_string.split(" ")
        filters = SearchQuery(search_string_list[0], config="french")
        if len(search_string_list) > 1:
            for search_term in search_string_list[1:]:
                filters |= SearchQuery(search_term, config="french")

        return self.filter(search_vector=filters).annotate(rank=SearchRank(F("search_vector"), filters))

    def filter_sectors(self, sectors):
        return self.filter(sectors__in=sectors)

    def filter_networks(self, networks):
        return self.filter(networks__in=networks)

    def filter_labels(self, labels):
        return self.filter(labels__in=labels)

    def has_user(self):
        """Only return siaes who have at least 1 User."""
        return self.filter(users__isnull=False).distinct()

    def has_sector(self):
        """Only return siaes who have at least 1 Sector."""
        return self.filter(sectors__isnull=False).distinct()

    def has_network(self):
        """Only return siaes who have at least 1 Network."""
        return self.filter(networks__isnull=False).distinct()

    def has_offer(self):
        """Only return siaes who have at least 1 SiaeOffer."""
        return self.filter(offers__isnull=False).distinct()

    def has_label(self):
        """Only return siaes who have at least 1 SiaeLabelOld."""
        return self.filter(labels_old__isnull=False).distinct()

    def has_client_reference(self):
        """Only return siaes who have at least 1 SiaeClientReference."""
        return self.filter(client_references__isnull=False).distinct()

    def in_region(self, **kwargs):
        if "region_name" in kwargs:
            return self.filter(region=kwargs["region_name"])
        if "region_code" in kwargs:
            code_clean = kwargs["region_code"].strip("R")  # "R" ? see Perimeter model & import_region.py
            region_name = siae_constants.REGIONS.get(code_clean)
            return self.filter(region=region_name)

    def in_department(self, **kwargs):
        if "depatment_name" in kwargs:
            department_code = siae_constants.get_department_code_from_name(kwargs["depatment_name"])
            return self.filter(department=department_code)
        if "department_code" in kwargs:
            return self.filter(department=kwargs["department_code"])

    def in_range_of_point(self, **kwargs):
        if "city_coords" in kwargs:
            # Doesn't work..
            # return self.filter(Q(geo_range=GEO_RANGE_CUSTOM) & Q(coords__dwithin=(kwargs["city_coords"], D(km=F("geo_range_custom_distance")))))  # noqa
            # Distance returns a number in meters. But geo_range_custom_distance is stored in km. So we divide by 1000  # noqa
            return self.filter(
                Q(geo_range=siae_constants.GEO_RANGE_CUSTOM)
                & Q(geo_range_custom_distance__lte=Distance("coords", kwargs["city_coords"]) / 1000)
            )

    def in_city_area(self, perimeter):
        return self.filter(get_city_filter(perimeter))

    def address_in_perimeter_list(self, perimeters: models.QuerySet):
        """
        Simple method to filter the Siaes depending on the perimeter filter.
        We only filter on the Siae's address field.
        """
        conditions = Q()
        for perimeter in perimeters:
            if perimeter.kind == Perimeter.KIND_CITY:
                conditions |= get_simple_city_filter(perimeter)
            if perimeter.kind == Perimeter.KIND_DEPARTMENT:
                conditions |= get_department_filter(perimeter)
            if perimeter.kind == Perimeter.KIND_REGION:
                conditions |= get_region_filter(perimeter)
        return self.filter(conditions)

    def geo_range_in_perimeter_list(self, perimeters: models.QuerySet, with_country=False):
        """
        Method to filter the Siaes depending on the perimeter filter.
        We filter on the Siae's address & geo_range fields.
        Depending on the type of Perimeter that were chosen, different cases arise:

        **CITY**
        return the Siae with the post code in perimeter.post_codes
        OR the Siae with the same department (except geo_range=GEO_RANGE_CUSTOM)
        OR the Siae with geo_range=GEO_RANGE_CUSTOM and a perimeter radius that overlaps with the city

        **DEPARTMENT**
        return only the Siae in this department

        **REGION**
        return only the Siae in this region
        """
        conditions = Q()
        for perimeter in perimeters:
            if perimeter.kind == Perimeter.KIND_CITY:
                # https://stackoverflow.com/questions/20222457/django-building-a-queryset-with-q-objects
                conditions |= get_city_filter(perimeter, with_country)
            if perimeter.kind == Perimeter.KIND_DEPARTMENT:
                conditions |= get_department_filter(perimeter)
            if perimeter.kind == Perimeter.KIND_REGION:
                conditions |= get_region_filter(perimeter)
        return self.filter(conditions)

    def within(self, point, distance_km=0):
        return self.filter(coords__dwithin=(point, D(km=distance_km)))

    def with_country_geo_range(self):
        return self.filter(Q(geo_range=siae_constants.GEO_RANGE_COUNTRY))

    def exclude_country_geo_range(self):
        return self.exclude(Q(geo_range=siae_constants.GEO_RANGE_COUNTRY))

    def annotate_with_user_favorite_list_count(self, user):
        """
        Enrich each Siae with the number of occurences in the user's favorite lists
        """
        return self.prefetch_related("favorite_lists").annotate(
            in_user_favorite_list_count=Count("favorite_lists", filter=Q(favorite_lists__user=user))
        )

    def annotate_with_user_favorite_list_ids(self, user):
        """
        Enrich each Siae with the list of occurences in the user's favorite lists
        """
        return self.prefetch_related("favorite_lists").annotate(
            in_user_favorite_list_ids=ArrayAgg("favorite_lists__pk", filter=Q(favorite_lists__user=user))
        )

    def has_contact_email(self):
        return self.exclude(contact_email__isnull=True).exclude(contact_email__exact="")

    def filter_with_tender(self, tender):
        """
        Filter Siaes with tenders:
        - first we filter the Siae that are live + can be contacted
        - then we filter on the sectors
        - then we filter on the perimeters:
            - if tender is made for country area, we filter with siae_geo_range=country
            - else we filter on the perimeters
        - then we filter on presta_type
        - finally we filter on kind

        Args:
            tender (Tender): Tender used to make the matching
        """
        qs = self.prefetch_related("sectors").is_live().has_contact_email()
        # filter by sectors
        if tender.sectors.count():
            qs = qs.filter_sectors(tender.sectors.all())
        # filter by perimeters
        if tender.is_country_area:
            qs = qs.with_country_geo_range()
        else:
            if tender.perimeters.count():
                qs = qs.geo_range_in_perimeter_list(tender.perimeters.all(), with_country=True)
            if not tender.include_country_area:
                qs = qs.exclude_country_geo_range()
        # filter by presta_type
        if len(tender.presta_type):
            qs = qs.filter(presta_type__overlap=tender.presta_type)
        # filter by siae_kind
        if len(tender.siae_kind):
            qs = qs.filter(kind__in=tender.siae_kind)
        # return
        return qs.distinct()

    def with_tender_stats(self):
        """
        Enrich each Siae with stats on their linked Tender
        """
        return self.annotate(
            tender_count=Count("tenders", distinct=True),
            tender_email_send_count=Sum(
                Case(When(tendersiae__email_send_date__isnull=False, then=1), default=0, output_field=IntegerField())
            ),
            tender_email_link_click_count=Sum(
                Case(
                    When(tendersiae__email_link_click_date__isnull=False, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            tender_detail_display_count=Sum(
                Case(
                    When(tendersiae__detail_display_date__isnull=False, then=1), default=0, output_field=IntegerField()
                )
            ),
            tender_detail_contact_click_count=Sum(
                Case(
                    When(tendersiae__detail_contact_click_date__isnull=False, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
        )

    def annotate_with_brand_or_name(self, with_order_by=False):
        """
        We usually want to display the brand by default
        See Siae.name_display()
        """
        qs = self.annotate(
            brand_or_name=Case(When(brand="", then=F("name")), default=F("brand"), output_field=CharField())
        )
        if with_order_by:
            qs = qs.order_by("brand_or_name")
        return qs

    def with_content_filled_stats(self):
        """
        Content fill levels:
        - Level 1 (basic): user_count + sector_count + description
        - Level 2: user_count + sector_count + description + logo + client_reference_count
        - Level 3: user_count + sector_count + description + logo + client_reference_count + image_count
        - Level 4 (full): user_count + sector_count + description + logo + client_reference_count + image_count + label_count  # noqa
        """
        return self.annotate(
            content_filled_basic=Case(
                When(Q(user_count__gte=1) & Q(sector_count__gte=1) & ~Q(description=""), then=True),
                default=False,
                output_field=BooleanField(),
            ),
            content_filled_full=Case(
                When(
                    Q(user_count__gte=1)
                    & Q(sector_count__gte=1)
                    & ~Q(description="")
                    & ~Q(logo_url="")
                    & Q(client_reference_count__gte=1)
                    & Q(image_count__gte=1)
                    & Q(label_count__gte=1),
                    then=True,
                ),
                default=False,
                output_field=BooleanField(),
            ),
        )

    def content_not_filled(self):
        return self.filter(Q(sector_count=0) | Q(contact_email=""))

    def with_employees_count(self):
        """
        Enrich each Siae with count of employees
        Annotate first to use the field "c2_etp_count" if employees_insertion_count is null
        Next, the sum of an integer and a null value is null, so we check if fields are not null before make sum
        """
        return self.annotate(
            employees_insertion_count_with_c2_etp=Case(
                When(employees_insertion_count=None, then=Round(F("c2_etp_count"))),
                default=F("employees_insertion_count"),
                output_field=PositiveIntegerField(),
            )
        ).annotate(
            employees_count=Case(
                When(employees_insertion_count_with_c2_etp=None, then=F("employees_permanent_count")),
                When(employees_permanent_count=None, then=F("employees_insertion_count_with_c2_etp")),
                default=F("employees_insertion_count_with_c2_etp") + F("employees_permanent_count"),
            )
        )


class Siae(models.Model):
    READONLY_FIELDS_FROM_C1 = [
        "name",
        "slug",  # generated from 'name'
        "brand",
        "siret",
        "naf",
        "website",
        "email",
        "phone",
        "kind",
        "nature",
        "address",
        "city",
        "post_code",
        "department",
        "region",
        "coords",
        "admin_name",
        "admin_email",
        "is_active",
        "siret_is_valid",
        "asp_id",
        "c1_id",
        "c4_id_old",
        "c1_last_sync_date",
        "source",
    ]
    READONLY_FIELDS_FROM_C2 = [
        "c2_etp_count",
        "c2_etp_count_date_saisie",
        "c2_etp_count_last_sync_date",
    ]
    READONLY_FIELDS_FROM_QPV = ["is_qpv", "qpv_name", "qpv_code", "api_qpv_last_sync_date"]
    READONLY_FIELDS_FROM_ZRR = ["is_zrr", "zrr_name", "zrr_code", "api_zrr_last_sync_date"]
    READONLY_FIELDS_FROM_API_ENTREPRISE = [
        "api_entreprise_forme_juridique",
        "api_entreprise_forme_juridique_code",
        "api_entreprise_entreprise_last_sync_date",
        "api_entreprise_date_constitution",
        "api_entreprise_employees",
        "api_entreprise_employees_year_reference",
        "api_entreprise_etablissement_last_sync_date",
        "api_entreprise_ca",
        "api_entreprise_ca_date_fin_exercice",
        "api_entreprise_exercice_last_sync_date",
    ]
    READONLY_FIELDS = (
        READONLY_FIELDS_FROM_C1
        + READONLY_FIELDS_FROM_C2
        + READONLY_FIELDS_FROM_QPV
        + READONLY_FIELDS_FROM_ZRR
        + READONLY_FIELDS_FROM_API_ENTREPRISE
    )

    TRACK_UPDATE_FIELDS = [
        # update coords
        "address",
        # set last_updated fields
        "employees_insertion_count",
        "employees_permanent_count",
        "ca",
    ]

    SOURCE_ASP = "ASP"
    SOURCE_GEIQ = "GEIQ"
    SOURCE_EA_EATT = "EA_EATT"
    SOURCE_USER_CREATED = "USER_CREATED"
    SOURCE_STAFF_C1_CREATED = "STAFF_C1_CREATED"
    SOURCE_STAFF_C4_CREATED = "STAFF_C4_CREATED"
    SOURCE_ESAT = "ESAT"
    SOURCE_SEP = "SEP"

    SOURCE_CHOICES = (
        (SOURCE_ASP, "Export ASP"),
        (SOURCE_GEIQ, "Export GEIQ"),
        (SOURCE_EA_EATT, "Export EA+EATT"),
        (SOURCE_USER_CREATED, "Utilisateur (Antenne)"),
        (SOURCE_STAFF_C1_CREATED, "Staff C1"),
        (SOURCE_STAFF_C4_CREATED, "Staff C4"),
        (SOURCE_ESAT, "Import ESAT (GSAT, Handeco)"),
        (SOURCE_SEP, "Import SEP"),
    )

    NATURE_HEAD_OFFICE = "HEAD_OFFICE"
    NATURE_ANTENNA = "ANTENNA"

    NATURE_CHOICES = (
        (NATURE_HEAD_OFFICE, "Conventionné par la DREETS"),
        (NATURE_ANTENNA, "Rattaché à un autre conventionnement"),
    )

    DEPARTMENT_CHOICES = DEPARTMENTS_PRETTY.items()
    REGION_CHOICES = REGIONS_PRETTY.items()

    name = models.CharField(verbose_name="Raison sociale", max_length=255)
    slug = models.SlugField(verbose_name="Slug", max_length=255, unique=True)
    brand = models.CharField(verbose_name="Enseigne", max_length=255, blank=True)
    kind = models.CharField(
        verbose_name="Type de structure",
        max_length=6,
        choices=siae_constants.KIND_CHOICES_WITH_EXTRA,
        default=siae_constants.KIND_EI,
        db_index=True,
    )
    description = models.TextField(verbose_name="Description", blank=True)
    siret = models.CharField(verbose_name="Siret", validators=[validate_siret], max_length=14, db_index=True)
    siret_is_valid = models.BooleanField(verbose_name="Siret Valide", default=False)
    naf = models.CharField(verbose_name="Naf", validators=[validate_naf], max_length=5, blank=True)
    nature = models.CharField(verbose_name="Établissement", max_length=20, choices=NATURE_CHOICES, blank=True)
    presta_type = ChoiceArrayField(
        verbose_name="Type de prestation",
        base_field=models.CharField(max_length=20, choices=siae_constants.PRESTA_CHOICES),
        blank=True,
        null=True,
        db_index=True,
    )
    legal_form = models.CharField(
        verbose_name="Forme juridique",
        max_length=20,
        choices=siae_constants.LEGAL_FORM_CHOICES,
        blank=True,
        db_index=True,
    )

    website = models.URLField(verbose_name="Site internet", blank=True)
    email = models.EmailField(verbose_name="E-mail", blank=True)
    phone = models.CharField(verbose_name="Téléphone", max_length=20, blank=True)

    address = models.TextField(verbose_name="Adresse")
    city = models.CharField(verbose_name="Ville", max_length=255, blank=True)
    # department is a code
    department = models.CharField(verbose_name="Département", max_length=255, choices=DEPARTMENT_CHOICES, blank=True)
    # region is a name
    region = models.CharField(verbose_name="Région", max_length=255, choices=REGION_CHOICES, blank=True)
    # post_code or insee_code ?
    post_code = models.CharField(verbose_name="Code Postal", validators=[validate_post_code], max_length=5, blank=True)
    # Latitude and longitude coordinates.
    # https://docs.djangoproject.com/en/2.2/ref/contrib/gis/model-api/#pointfield
    coords = gis_models.PointField(geography=True, blank=True, null=True)
    geo_range = models.CharField(
        verbose_name="Périmètre d'intervention",
        max_length=20,
        choices=siae_constants.GEO_RANGE_CHOICES,
        blank=True,
        db_index=True,
    )
    geo_range_custom_distance = models.IntegerField(
        verbose_name="Distance en kilomètres (périmètre d'intervention)", blank=True, null=True
    )

    contact_first_name = models.CharField(verbose_name="Prénom", max_length=150, blank=True)
    contact_last_name = models.CharField(verbose_name="Nom", max_length=150, blank=True)
    contact_website = models.URLField(
        verbose_name="Site internet", help_text="Doit commencer par http:// ou https://", blank=True
    )
    contact_email = models.EmailField(
        verbose_name="E-mail",
        blank=True,
        help_text="Le contact renseigné ici recevra les opportunités commerciales par mail",
    )
    contact_phone = models.CharField(verbose_name="Téléphone", max_length=150, blank=True)
    contact_social_website = models.URLField(
        verbose_name="Réseau social", help_text="Doit commencer par http:// ou https://", blank=True
    )

    image_name = models.CharField(verbose_name="Nom de l'image", max_length=255, blank=True)
    logo_url = models.URLField(verbose_name="Lien vers le logo", max_length=500, blank=True)

    is_consortium = models.BooleanField(verbose_name="Consortium", default=False)
    is_cocontracting = models.BooleanField(verbose_name="Co-traitance", default=False)

    asp_id = models.IntegerField(verbose_name="ID ASP", blank=True, null=True)
    is_active = models.BooleanField(
        verbose_name="Active", help_text="Convention active (C1) ou import", default=True, db_index=True
    )
    is_delisted = models.BooleanField(
        verbose_name="Masquée",
        help_text="La structure n'apparaîtra plus dans les résultats",
        default=False,
        db_index=True,
    )
    is_first_page = models.BooleanField(
        verbose_name="A la une", help_text="La structure apparaîtra sur la page principale", default=False
    )

    admin_name = models.CharField(max_length=255, blank=True)
    admin_email = models.EmailField(max_length=255, blank=True)

    year_constitution = models.PositiveIntegerField(verbose_name="Année de création", blank=True, null=True)
    employees_insertion_count = models.PositiveIntegerField(
        verbose_name="Nombre de salariés en insertion", blank=True, null=True
    )
    employees_insertion_count_last_updated = models.DateTimeField(
        verbose_name="Date de dernière mise à jour du nombre de salariés en insertion", blank=True, null=True
    )
    employees_permanent_count = models.PositiveIntegerField(
        verbose_name="Nombre de salariés permanents", blank=True, null=True
    )
    employees_permanent_count_last_updated = models.DateTimeField(
        verbose_name="Date de dernière mise à jour du nombre de salariés permanents", blank=True, null=True
    )
    ca = models.PositiveIntegerField(verbose_name="Chiffre d'affaires", blank=True, null=True)
    ca_last_updated = models.DateTimeField(
        verbose_name="Date de dernière mise à jour du chiffre d'affaires", blank=True, null=True
    )

    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="siaes.SiaeUser",
        verbose_name="Gestionnaires",
        related_name="siaes",
        blank=True,
    )
    sectors = models.ManyToManyField(
        "sectors.Sector", verbose_name="Secteurs d'activité", related_name="siaes", blank=True
    )
    networks = models.ManyToManyField("networks.Network", verbose_name="Réseaux", related_name="siaes", blank=True)
    groups = models.ManyToManyField("siaes.SiaeGroup", verbose_name="Groupements", related_name="siaes", blank=True)
    labels = models.ManyToManyField(
        "labels.Label",
        through="siaes.SiaeLabel",
        verbose_name="Labels & certifications",
        related_name="siaes",
        blank=True,
    )
    # ForeignKeys: offers, client_references, labels_old, images

    # C2 (ETP)
    c2_etp_count = models.FloatField("Nombre d'ETP (C2)", blank=True, null=True)
    c2_etp_count_date_saisie = models.DateField("Date de saisie du nombre d'ETP (C2)", blank=True, null=True)
    c2_etp_count_last_sync_date = models.DateTimeField(
        "Date de dernière synchronisation (C2 ETP)", blank=True, null=True
    )

    # API QPV
    is_qpv = models.BooleanField(
        verbose_name="Quartier prioritaire de la politique de la ville (API QPV)",
        blank=False,
        null=False,
        default=False,
        db_index=True,
    )
    # To avoid QPV zones synchro problematics, we take the choice to duplicate names and codes of QPV
    qpv_name = models.CharField(verbose_name="Nom de la zone QPV (API QPV)", max_length=255, blank=True)
    qpv_code = models.CharField(verbose_name="Code de la zone QPV (API QPV)", max_length=16, blank=True)
    api_qpv_last_sync_date = models.DateTimeField("Date de dernière synchronisation (API QPV)", blank=True, null=True)

    # API ZRR
    # To avoid ZRR zones synchro problematics, we take the choice to duplicate names and codes of ZRR
    is_zrr = models.BooleanField(
        verbose_name="Zone de revitalisation rurale (API ZRR)", blank=False, null=False, default=False, db_index=True
    )
    zrr_name = models.CharField(verbose_name="Nom de la zone ZRR (API ZRR)", max_length=255, blank=True)
    zrr_code = models.CharField(verbose_name="Code de la zone ZRR (API ZRR)", max_length=16, blank=True)
    api_zrr_last_sync_date = models.DateTimeField("Date de dernière synchronisation (API ZRR)", blank=True, null=True)

    # API Entreprise
    api_entreprise_forme_juridique = models.CharField(
        verbose_name="Forme juridique (API Entreprise)", max_length=255, blank=True
    )
    api_entreprise_forme_juridique_code = models.CharField(
        verbose_name="Code de la forme juridique (API Entreprise)", max_length=5, blank=True
    )
    api_entreprise_entreprise_last_sync_date = models.DateTimeField(
        "Date de dernière synchronisation (API Entreprise /entreprises)", blank=True, null=True
    )
    api_entreprise_date_constitution = models.DateField(
        verbose_name="Date de création (API Entreprise)", blank=True, null=True
    )
    api_entreprise_employees = models.CharField(
        verbose_name="Nombre de salariés (API Entreprise)", max_length=255, blank=True
    )
    api_entreprise_employees_year_reference = models.CharField(
        verbose_name="Année de référence du nombre de salariés (API Entreprise)", max_length=4, blank=True
    )
    api_entreprise_etablissement_last_sync_date = models.DateTimeField(
        "Date de dernière synchronisation (API Entreprise /etablissements)", blank=True, null=True
    )
    api_entreprise_ca = models.IntegerField(verbose_name="Chiffre d'affaires (API Entreprise)", blank=True, null=True)
    api_entreprise_ca_date_fin_exercice = models.DateField(
        verbose_name="Date de fin de l'exercice (API Entreprise)", blank=True, null=True
    )
    api_entreprise_exercice_last_sync_date = models.DateTimeField(
        "Date de dernière synchronisation (API Entreprise /exercices)", blank=True, null=True
    )

    c1_id = models.IntegerField(blank=True, null=True)
    c4_id_old = models.IntegerField(blank=True, null=True)
    c1_last_sync_date = models.DateTimeField(blank=True, null=True)
    c1_sync_skip = models.BooleanField(blank=False, null=False, default=False)

    search_vector = SearchVectorField("Search vector", null=True)

    # admin
    notes = GenericRelation("notes.Note", related_query_name="siae")

    # stats
    user_count = models.IntegerField("Nombre d'utilisateurs", default=0)
    sector_count = models.IntegerField("Nombre de secteurs d'activité", default=0)
    network_count = models.IntegerField("Nombre de réseaux", default=0)
    group_count = models.IntegerField("Nombre de groupements", default=0)
    offer_count = models.IntegerField("Nombre de prestations", default=0)
    client_reference_count = models.IntegerField("Nombre de références clients", default=0)
    label_count = models.IntegerField("Nombre de labels", default=0)
    image_count = models.IntegerField("Nombre d'images", default=0)
    signup_date = models.DateTimeField(
        "Date d'inscription de la structure (premier utilisateur)", blank=True, null=True
    )
    content_filled_basic_date = models.DateTimeField(
        "Date de remplissage (basique) de la fiche", blank=True, null=True
    )
    logs = models.JSONField(verbose_name="Logs historiques", editable=False, default=list)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_STAFF_C4_CREATED)
    import_raw_object = models.JSONField(verbose_name="Donnée JSON brute", editable=False, null=True)

    created_at = models.DateTimeField(verbose_name="Date de création", default=timezone.now)
    updated_at = models.DateTimeField(verbose_name="Date de mise à jour", auto_now=True)

    objects = models.Manager.from_queryset(SiaeQuerySet)()

    class Meta:
        verbose_name = "Structure"
        verbose_name_plural = "Structures"
        indexes = [
            GinIndex(fields=["search_vector"]),
        ]
        ordering = ["name"]

    def __str__(self):
        return self.name

    def __init__(self, *args, **kwargs):
        """
        https://stackoverflow.com/a/23363123
        """
        super(Siae, self).__init__(*args, **kwargs)
        for field_name in self.TRACK_UPDATE_FIELDS:
            setattr(self, f"__previous_{field_name}", getattr(self, field_name))

    def set_slug(self, with_uuid=False):
        """
        The slug field should be unique.
        Some SIAE have duplicate name, so we suffix the slug with their department.
        In some rare cases, name+department is not enough, so we add 4 random characters at the end.
        """
        if not self.slug:
            self.slug = f"{slugify(self.name)[:40]}-{str(self.department or '')}"
        if with_uuid:
            self.slug += f"-{str(uuid4())[:4]}"

    def set_last_updated_fields(self):
        """
        We track changes on some fields, in order to update their 'last_updated' counterpart.
        Where are the '__previous' fields set? In the __init__ method
        """
        for field_name in self.TRACK_UPDATE_FIELDS:
            previous_field_name = f"__previous_{field_name}"
            if getattr(self, field_name) and getattr(self, field_name) != getattr(self, previous_field_name):
                try:
                    setattr(self, f"{field_name}_last_updated", timezone.now())
                except AttributeError:  # TRACK_UPDATE_FIELDS without last_updated fields
                    pass

    def set_related_counts(self):
        """
        Works only for related fields.
        For M2M, see m2m_changed signal.
        """
        if self.id:
            self.offer_count = self.offers.count()
            self.client_reference_count = self.client_references.count()
            self.label_count = self.labels_old.count()
            self.image_count = self.images.count()
            # user_count, sector_count, network_count, group_count? see M2M signals

    def set_content_fill_dates(self):
        """
        Content fill levels?
        See with_content_filled_stats()
        """
        if self.id:
            if all(getattr(self, field) for field in ["user_count", "sector_count", "description"]):
                if not self.content_filled_basic_date:
                    self.content_filled_basic_date = timezone.now()
            # else:
            #     if self.content_filled_basic_date:
            #         self.content_filled_basic_date = None

    def save(self, *args, **kwargs):
        """
        - update the "last_updated" fields
        - update the object stats
        - update the object content_fill_dates
        - generate the slug field
        """
        self.set_last_updated_fields()
        self.set_related_counts()
        self.set_content_fill_dates()
        try:
            self.set_slug()
            with transaction.atomic():
                super().save(*args, **kwargs)
        except IntegrityError as e:
            # check that it's a slug conflict
            # Full message expected: duplicate key value violates unique constraint "siaes_siae_slug_0f0b821f_uniq" DETAIL:  Key (slug)=(...) already exists.  # noqa
            if "siaes_siae_slug" in str(e):
                self.set_slug(with_uuid=True)
                super().save(*args, **kwargs)
            else:
                raise e

    @property
    def latitude(self):
        if self.coords:
            return self.coords.y
        return None

    @property
    def longitude(self):
        if self.coords:
            return self.coords.x
        return None

    @property
    def name_display(self):
        if self.brand:
            return self.brand
        return self.name

    @property
    def presta_type_display(self):
        if self.kind == siae_constants.KIND_ETTI:
            return "Intérim"
        if self.kind == siae_constants.KIND_AI:
            return "Mise à disposition du personnel"
        if self.presta_type:
            presta_type_values = [
                force_str(dict(siae_constants.PRESTA_CHOICES).get(key, "")) for key in self.presta_type
            ]
            return ", ".join(filter(None, presta_type_values))
        return ""

    @property
    def siret_display(self):
        """
        SIRET = 14 numbers
        SIREN = 9 numbers
        SIREN + NIC = SIRET
        """
        if len(self.siret) == 14:
            return f"{self.siret[0:3]} {self.siret[3:6]} {self.siret[6:9]} {self.siret[9:14]}"
        if len(self.siret) == 9:
            return f"{self.siret[0:3]} {self.siret[3:6]} {self.siret[6:9]}"
        return self.siret

    @property
    def year_constitution_display(self):
        if self.year_constitution:
            return self.year_constitution
        if self.api_entreprise_date_constitution:
            return self.api_entreprise_date_constitution.year
        return "non disponible"

    @property
    def ca_display(self):
        if self.ca or self.api_entreprise_ca:
            ca = self.ca or self.api_entreprise_ca
            # https://stackoverflow.com/a/18891054/4293684
            ca_formatted = "{:,}".format(ca).replace(",", " ")
            return f"{ca_formatted}€"
        return "non disponible"

    @property
    def etp_count_label_display(self):
        if self.kind_is_esat_or_ea_or_eatt:
            return "Travailleurs en situation de handicap"
        return "Salariés en insertion"

    @property
    def etp_count_display(self):
        if self.employees_insertion_count:
            return self.employees_insertion_count
        elif self.c2_etp_count:
            return self.c2_etp_count

    @property
    def contact_full_name(self):
        if self.contact_first_name and self.contact_last_name:
            return f"{self.contact_first_name} {self.contact_last_name}"
        return ""

    @property
    def contact_short_name(self):
        if self.contact_first_name and self.contact_last_name:
            return f"{self.contact_first_name.upper()[:1]}. {self.contact_last_name.upper()}"
        return ""

    @property
    def geo_range_pretty_display(self):
        if self.geo_range == siae_constants.GEO_RANGE_COUNTRY:
            return self.get_geo_range_display()
        elif self.geo_range == siae_constants.GEO_RANGE_REGION:
            return f"{self.get_geo_range_display().lower()} ({self.region})"
        elif self.geo_range == siae_constants.GEO_RANGE_DEPARTMENT:
            return f"{self.get_geo_range_display().lower()} ({self.department})"
        elif self.geo_range == siae_constants.GEO_RANGE_CUSTOM:
            if self.geo_range_custom_distance:
                return f"{self.geo_range_custom_distance} km"
        return "non disponible"

    @property
    def geo_range_pretty_title(self):
        if self.geo_range == siae_constants.GEO_RANGE_COUNTRY:
            return self.geo_range_pretty_display
        elif self.geo_range == siae_constants.GEO_RANGE_REGION:
            return self.region
        elif self.geo_range == siae_constants.GEO_RANGE_DEPARTMENT:
            return self.get_department_display()
        elif self.geo_range == siae_constants.GEO_RANGE_CUSTOM:
            if self.geo_range_custom_distance:
                return f"{self.geo_range_pretty_display} de {self.city}"
        return self.geo_range_pretty_display

    @property
    def is_missing_contact(self):
        """
        Return True if all of the contact fields are missing
        """
        return not any(
            getattr(self, field)
            for field in ["contact_website", "contact_email", "contact_phone", "contact_social_website"]
        )

    @property
    def is_missing_content(self):
        has_other_fields = all(
            getattr(self, field)
            for field in [
                "description",
                "sector_count",
                "offer_count",
            ]
        )
        return self.is_missing_contact or not has_other_fields

    @property
    def source_display(self):
        if self.kind == siae_constants.KIND_ESAT:
            return "GESAT/Handeco"
        elif self.kind == siae_constants.KIND_SEP:
            return "l'ATIGIP"
        else:
            return "l'ASP"

    @property
    def kind_is_esat_or_ea_or_eatt(self):
        return self.kind in [siae_constants.KIND_ESAT, siae_constants.KIND_EA, siae_constants.KIND_EATT]

    @property
    def completion_percent(self):
        score, total = 0, 0
        for key, value in siae_constants.SIAE_COMPLETION_SCORE_GRID.items():
            completion_item_kind = value[siae_constants.COMPLETION_KIND_KEY]
            score_item = value[siae_constants.COMPLETION_SCORE_KEY]
            if completion_item_kind == siae_constants.COMPLETION_KIND_NOT_EMPTY_OR_FALSE:
                if getattr(self, key):
                    score += score_item
            elif completion_item_kind == siae_constants.COMPLETION_KIND_GREATER_THAN:
                if getattr(self, key) and getattr(self, key) > value[siae_constants.COMPLETION_COMPARE_TO_KEY]:
                    score += score_item
            total += score_item
        score_percent = round(score / total, 2) * 100
        return round_by_base(score_percent, base=5)

    def sectors_list_string(self, display_max=5):
        sectors_name_list = self.sectors.form_filter_queryset().values_list("name", flat=True)
        if display_max and len(sectors_name_list) > display_max:
            sectors_name_list = sectors_name_list[:display_max]
            sectors_name_list.append("…")
        return ", ".join(sectors_name_list)

    def sectors_full_list_string(self):
        return self.sectors_list_string(display_max=None)

    @cached_property
    def stat_view_count_last_3_months(self):
        try:
            return Tracker.objects.siae_views_last_3_months(self.slug).count()
        except:  # noqa
            return "-"

    @cached_property
    def stat_buyer_view_count_last_3_months(self):
        try:
            return Tracker.objects.siae_buyer_views_last_3_months(self.slug).count()
        except:  # noqa
            return "-"

    @cached_property
    def stat_partner_view_count_last_3_months(self):
        try:
            return Tracker.objects.siae_partner_views_last_3_months(self.slug).count()
        except:  # noqa
            return "-"

    def siae_user_requests_pending_count(self):
        # TODO: optimize + filter on assignee
        return self.siaeuserrequest_set.pending().count()

    def get_absolute_url(self):
        return reverse("siae:detail", kwargs={"slug": self.slug})


@receiver(post_save, sender=Siae)
def siae_post_save(sender, instance, **kwargs):
    field_name = "address"
    previous_field_name = f"__previous_{field_name}"
    if getattr(instance, field_name) and getattr(instance, field_name) != getattr(instance, previous_field_name):
        set_siae_coords(sender, instance)


@receiver(m2m_changed, sender=Siae.users.through)
def siae_users_changed(sender, instance, action, **kwargs):
    """
    Why do we need this? (looks like a duplicate of siae_siaeusers_changed)
    Will be called if we do `siae.users.add(user)` or `user.siaes.add(siae)` (also .set())
    """
    if action in ("post_add", "post_remove", "post_clear"):
        if isinstance(instance, User):
            # if we do user.siaes.add(siae), we get a User instance
            # we need to transform it to an Siae instance
            # TODO: manage the case where user.siaes.set([siae1, siae2]) ...
            siae_id = next(iter(kwargs["pk_set"]))
            instance = Siae.objects.get(id=siae_id)
        instance.user_count = instance.users.count()
        if instance.user_count > 0 and not instance.signup_date:
            instance.signup_date = timezone.now()
        instance.save()


@receiver(m2m_changed, sender=Siae.sectors.through)
def siae_sectors_changed(sender, instance, action, **kwargs):
    if action in ("post_add", "post_remove", "post_clear"):
        instance.sector_count = instance.sectors.count()
        instance.save()


@receiver(m2m_changed, sender=Siae.networks.through)
def siae_networks_changed(sender, instance, action, **kwargs):
    if action in ("post_add", "post_remove", "post_clear"):
        instance.network_count = instance.networks.count()
        instance.save()


@receiver(m2m_changed, sender=Siae.groups.through)
def siae_groups_changed(sender, instance, action, **kwargs):
    if action in ("post_add", "post_remove", "post_clear"):
        instance.group_count = instance.groups.count()
        instance.save()


class SiaeUser(models.Model):
    siae = models.ForeignKey("siaes.Siae", verbose_name="Structure", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="Utilisateur", on_delete=models.CASCADE)

    created_at = models.DateTimeField(verbose_name="Date de création", default=timezone.now)
    updated_at = models.DateTimeField(verbose_name="Date de modification", auto_now=True)

    class Meta:
        verbose_name = "Gestionnaire"
        verbose_name_plural = "Gestionnaires"
        ordering = ["-created_at"]


@receiver(post_save, sender=SiaeUser)
@receiver(post_delete, sender=SiaeUser)
def siae_siaeusers_changed(sender, instance, **kwargs):
    """
    Will be called when we update the Siae form in the admin
    """
    instance.siae.user_count = instance.siae.users.count()
    if instance.siae.user_count > 0 and not instance.siae.signup_date:
        instance.siae.signup_date = timezone.now()
    instance.siae.save()


class SiaeUserRequestQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(response=None)

    def initiator(self, user):
        return self.filter(initiator=user)

    def assignee(self, user):
        return self.filter(assignee=user)


class SiaeUserRequest(models.Model):
    siae = models.ForeignKey("siaes.Siae", verbose_name="Structure", on_delete=models.CASCADE)
    initiator = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="Initiateur", on_delete=models.CASCADE)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Responsable",
        related_name="siaeuserrequest_assignee",
        on_delete=models.CASCADE,
    )

    response = models.BooleanField(verbose_name="Réponse", blank=True, null=True)
    response_date = models.DateTimeField("Date de la réponse", blank=True, null=True)

    logs = models.JSONField(verbose_name="Logs des échanges", editable=False, default=list)

    created_at = models.DateTimeField(verbose_name="Date de création", default=timezone.now)
    updated_at = models.DateTimeField(verbose_name="Date de modification", auto_now=True)

    objects = models.Manager.from_queryset(SiaeUserRequestQuerySet)()

    class Meta:
        verbose_name = "Demande de rattachement"
        verbose_name_plural = "Demandes de rattachement"
        ordering = ["-created_at"]


class SiaeOffer(models.Model):
    name = models.CharField(verbose_name="Nom", max_length=255)
    description = models.TextField(verbose_name="Description", blank=True)

    siae = models.ForeignKey("siaes.Siae", verbose_name="Structure", related_name="offers", on_delete=models.CASCADE)
    source = models.CharField(verbose_name="Source", max_length=20, blank=True)  # "listing_import"

    created_at = models.DateTimeField(verbose_name="Date de création", default=timezone.now)
    updated_at = models.DateTimeField(verbose_name="Date de modification", auto_now=True)

    class Meta:
        verbose_name = "Prestation"
        verbose_name_plural = "Prestations"

    def __str__(self):
        return self.name


class SiaeClientReference(models.Model):
    name = models.CharField(verbose_name="Nom", max_length=255, blank=True)
    description = models.TextField(verbose_name="Description", blank=True)
    image_name = models.CharField(verbose_name="Nom de l'image", max_length=255)
    logo_url = models.URLField(verbose_name="Lien vers le logo", max_length=500, blank=True)
    order = models.PositiveIntegerField(verbose_name="Ordre", blank=False, default=1)

    siae = models.ForeignKey(
        "siaes.Siae", verbose_name="Structure", related_name="client_references", on_delete=models.CASCADE
    )

    created_at = models.DateTimeField(verbose_name="Date de création", default=timezone.now)
    updated_at = models.DateTimeField(verbose_name="Date de modification", auto_now=True)

    class Meta:
        verbose_name = "Référence client"
        verbose_name_plural = "Références clients"

    # def __str__(self):
    #     if self.name:
    #         return self.name
    #     return f"SiaeClientReference object ({self.id})"


class SiaeLabel(models.Model):
    siae = models.ForeignKey("siaes.Siae", verbose_name="Structure", on_delete=models.CASCADE)
    label = models.ForeignKey("labels.Label", verbose_name="Label & certification", on_delete=models.CASCADE)

    logs = models.JSONField(verbose_name="Logs historiques", editable=False, default=list)

    created_at = models.DateTimeField(verbose_name="Date de création", default=timezone.now)
    updated_at = models.DateTimeField(verbose_name="Date de modification", auto_now=True)

    class Meta:
        verbose_name = "Label & certification"
        verbose_name_plural = "Labels & certifications"
        ordering = ["-created_at"]


class SiaeLabelOld(models.Model):
    name = models.CharField(verbose_name="Nom", max_length=255)

    siae = models.ForeignKey(
        "siaes.Siae", verbose_name="Structure", related_name="labels_old", on_delete=models.CASCADE
    )

    created_at = models.DateTimeField(verbose_name="Date de création", default=timezone.now)
    updated_at = models.DateTimeField(verbose_name="Date de modification", auto_now=True)

    class Meta:
        verbose_name = "Label & certification (old)"
        verbose_name_plural = "Labels & certifications (old)"
        # ordering = ["id"]

    def __str__(self):
        return self.name


class SiaeImage(models.Model):
    name = models.CharField(verbose_name="Nom", max_length=255, blank=True)
    description = models.TextField(verbose_name="Description", blank=True)
    image_name = models.CharField(verbose_name="Nom de l'image", max_length=255)
    image_url = models.URLField(verbose_name="Lien vers l'image", max_length=500, blank=True)
    order = models.PositiveIntegerField(verbose_name="Ordre", blank=False, default=1)

    c4_listing_id = models.IntegerField(blank=True, null=True)

    siae = models.ForeignKey("siaes.Siae", verbose_name="Structure", related_name="images", on_delete=models.CASCADE)

    created_at = models.DateTimeField("Date de création", default=timezone.now)
    updated_at = models.DateTimeField("Date de modification", auto_now=True)

    class Meta:
        verbose_name = "Image"
        verbose_name_plural = "Images"

    # def __str__(self):
    #     if self.name:
    #         return self.name
