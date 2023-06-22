from rest_framework import serializers

from lemarche.perimeters.models import Perimeter
from lemarche.sectors.models import Sector
from lemarche.tenders.models import Tender


class TenderSerializer(serializers.ModelSerializer):
    slug = serializers.CharField(read_only=True)
    sectors = serializers.SlugRelatedField(
        queryset=Sector.objects.all(), slug_field="slug", many=True, allow_null=True, required=False
    )
    location = serializers.SlugRelatedField(
        queryset=Perimeter.objects.all(), slug_field="slug", allow_null=True, required=False
    )
    contact_company_name = serializers.CharField(required=False)
    extra_data = serializers.JSONField(required=False)

    class Meta:
        model = Tender
        fields = [
            "slug",
            # general
            "kind",
            "title",
            "sectors",
            "presta_type",
            "location",
            "is_country_area",
            # description
            "description",
            "start_working_date",
            "external_link",
            "constraints",
            "amount",
            "why_amount_is_blank",
            "accept_share_amount",
            "accept_cocontracting",
            "siae_kind",
            # contact
            "contact_first_name",
            "contact_last_name",
            "contact_email",
            "contact_phone",
            "contact_company_name",
            "response_kind",
            "deadline_date",
            # extra data
            "extra_data",
        ]
