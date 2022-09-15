import json
import typing
from collections import defaultdict
from dataclasses import dataclass

import jsonschema
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.text import slugify
from rest_framework import serializers
from rest_framework_dataclasses.serializers import DataclassSerializer

from experimenter.base.models import (
    Country,
    Language,
    Locale,
    SiteFlag,
    SiteFlagNameChoices,
)
from experimenter.experiments.changelog_utils import generate_nimbus_changelog
from experimenter.experiments.constants import NimbusConstants
from experimenter.experiments.models import (
    NimbusBranch,
    NimbusBranchFeatureValue,
    NimbusBranchScreenshot,
    NimbusDocumentationLink,
    NimbusExperiment,
    NimbusFeatureConfig,
)
from experimenter.kinto.tasks import (
    nimbus_check_kinto_push_queue_by_collection,
    nimbus_synchronize_preview_experiments_in_kinto,
)
from experimenter.outcomes import Outcomes


class NestedRefResolver(jsonschema.RefResolver):
    """A custom ref resolver that handles bundled schema."""

    def __init__(self, schema):
        super().__init__(base_uri=None, referrer=None)

        if "$id" in schema:
            self.store[schema["$id"]] = schema

        if "$defs" in schema:
            for dfn in schema["$defs"].values():
                if "$id" in dfn:
                    self.store[dfn["$id"]] = dfn


@dataclass
class LabelValueDataClass:
    label: str
    value: str


@dataclass
class ApplicationConfigDataClass:
    application: str
    channels: typing.List[LabelValueDataClass]


@dataclass
class GeoDataClass:
    id: int
    name: str
    code: str


@dataclass
class FeatureConfigDataClass:
    id: int
    name: str
    slug: str
    description: str
    application: str
    ownerEmail: str
    schema: str


@dataclass
class UserDataClass:
    username: str


@dataclass
class TargetingConfigDataClass:
    label: str
    value: str
    applicationValues: typing.List[str]
    description: str
    stickyRequired: bool
    isFirstRunRequired: bool


@dataclass
class MetricDataClass:
    slug: str
    friendlyName: str
    description: str


@dataclass
class OutcomeDataClass:
    application: str
    description: str
    friendlyName: str
    slug: str
    isDefault: bool
    metrics: typing.List[MetricDataClass]


@dataclass
class NimbusConfigurationDataClass:
    applications: typing.List[LabelValueDataClass]
    channels: typing.List[LabelValueDataClass]
    applicationConfigs: typing.List[ApplicationConfigDataClass]
    countries: typing.List[GeoDataClass]
    locales: typing.List[GeoDataClass]
    languages: typing.List[GeoDataClass]
    documentationLink: typing.List[LabelValueDataClass]
    allFeatureConfigs: typing.List[FeatureConfigDataClass]
    firefoxVersions: typing.List[LabelValueDataClass]
    outcomes: typing.List[OutcomeDataClass]
    owners: typing.List[UserDataClass]
    targetingConfigs: typing.List[TargetingConfigDataClass]
    conclusionRecommendations: typing.List[LabelValueDataClass]
    types: typing.List[LabelValueDataClass]
    hypothesisDefault: str = NimbusExperiment.HYPOTHESIS_DEFAULT
    maxPrimaryOutcomes: int = NimbusExperiment.MAX_PRIMARY_OUTCOMES

    def __init__(self):
        self.applications = self._enum_to_label_value(NimbusExperiment.Application)
        self.channels = self._enum_to_label_value(NimbusExperiment.Channel)
        self.applicationConfigs = self._get_application_configs()
        self.countries = self._geo_model_to_dataclass(
            Country.objects.all().order_by("name")
        )
        self.locales = self._geo_model_to_dataclass(Locale.objects.all().order_by("name"))
        self.languages = self._geo_model_to_dataclass(
            Language.objects.all().order_by("name")
        )
        self.documentationLink = self._enum_to_label_value(
            NimbusExperiment.DocumentationLink
        )
        self.allFeatureConfigs = self._get_feature_configs()
        self.firefoxVersions = self._enum_to_label_value(NimbusExperiment.Version)
        self.outcomes = self._get_outcomes()
        self.owners = self._get_owners()
        self.targetingConfigs = self._get_targeting_configs()
        self.conclusionRecommendations = self._enum_to_label_value(
            NimbusExperiment.ConclusionRecommendation
        )
        self.types = self._enum_to_label_value(NimbusExperiment.Type)

    def _geo_model_to_dataclass(self, queryset):
        return [GeoDataClass(id=i.id, name=i.name, code=i.code) for i in queryset]

    def _enum_to_label_value(self, text_choices):
        return [
            LabelValueDataClass(
                label=text_choices[name].label,
                value=name,
            )
            for name in text_choices.names
        ]

    def _get_application_configs(self):
        configs = []
        for application in NimbusExperiment.Application:
            application_config = NimbusExperiment.APPLICATION_CONFIGS[application]
            configs.append(
                ApplicationConfigDataClass(
                    application=application.name,
                    channels=[
                        LabelValueDataClass(label=channel.label, value=channel.name)
                        for channel in NimbusExperiment.Channel
                        if channel in application_config.channel_app_id
                    ],
                )
            )
        return configs

    def _get_feature_configs(self):
        return [
            FeatureConfigDataClass(
                id=f.id,
                name=f.name,
                slug=f.slug,
                description=f.description,
                application=NimbusExperiment.Application(f.application).name,
                ownerEmail=f.owner_email,
                schema=f.schema,
            )
            for f in NimbusFeatureConfig.objects.all().order_by("name")
        ]

    def _get_owners(self):
        owners = (
            get_user_model()
            .objects.filter(owned_nimbusexperiments__isnull=False)
            .distinct()
            .order_by("email")
        )
        return [UserDataClass(username=owner.username) for owner in owners]

    def _get_targeting_configs(self):
        return [
            TargetingConfigDataClass(
                label=choice.label,
                value=choice.value,
                applicationValues=NimbusExperiment.TARGETING_CONFIGS[
                    choice.value
                ].application_choice_names,
                description=NimbusExperiment.TARGETING_CONFIGS[choice.value].description,
                stickyRequired=NimbusExperiment.TARGETING_CONFIGS[
                    choice.value
                ].sticky_required,
                isFirstRunRequired=NimbusExperiment.TARGETING_CONFIGS[
                    choice.value
                ].is_first_run_required,
            )
            for choice in NimbusExperiment.TargetingConfig
        ]

    def _get_outcomes(self):
        return [
            OutcomeDataClass(
                slug=outcome.slug,
                friendlyName=outcome.friendly_name,
                application=NimbusExperiment.Application(outcome.application).name,
                description=outcome.description,
                isDefault=outcome.is_default,
                metrics=[
                    MetricDataClass(
                        slug=metric.slug,
                        friendlyName=metric.friendly_name,
                        description=metric.description,
                    )
                    for metric in outcome.metrics
                ],
            )
            for outcome in Outcomes.all()
        ]


class NimbusConfigurationSerializer(DataclassSerializer):
    class Meta:
        dataclass = NimbusConfigurationDataClass


class ExperimentNameValidatorMixin:
    def validate_name(self, name):
        if not (self.instance or name):
            raise serializers.ValidationError("Name is required to create an experiment")

        if name:
            slug = slugify(name)

            if not slug:
                raise serializers.ValidationError(
                    "Name needs to contain alphanumeric characters"
                )

            if (
                self.instance is None
                and slug
                and NimbusExperiment.objects.filter(slug=slug).exists()
            ):
                raise serializers.ValidationError(
                    "Name maps to a pre-existing slug, please choose another name"
                )

        return name


class NimbusBranchScreenshotSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    description = serializers.CharField(max_length=1024, required=False, allow_blank=True)
    image = serializers.ImageField(required=False, allow_null=True, allow_empty_file=True)

    class Meta:
        model = NimbusBranchScreenshot
        fields = (
            "id",
            "description",
            "image",
        )


class NimbusBranchFeatureValueSerializer(serializers.ModelSerializer):
    featureConfig = serializers.PrimaryKeyRelatedField(
        source="feature_config",
        queryset=NimbusFeatureConfig.objects.all(),
        required=False,
        allow_null=False,
    )
    enabled = serializers.BooleanField(required=False)
    value = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = NimbusBranchFeatureValue
        fields = (
            "featureConfig",
            "enabled",
            "value",
        )

    def validate(self, data):
        if data.get("enabled") and not data.get("value"):
            raise serializers.ValidationError(
                {"value": NimbusConstants.ERROR_BRANCH_NO_VALUE}
            )

        if data.get("value") and not data.get("enabled"):
            raise serializers.ValidationError(
                {"enabled": NimbusConstants.ERROR_BRANCH_NO_ENABLED}
            )

        return data


class NimbusBranchSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    screenshots = NimbusBranchScreenshotSerializer(many=True, required=False)
    featureEnabled = serializers.BooleanField(required=False, write_only=True)
    featureValue = serializers.CharField(
        required=False, allow_blank=True, write_only=True
    )
    featureValues = NimbusBranchFeatureValueSerializer(many=True, required=False)

    class Meta:
        model = NimbusBranch
        fields = (
            "id",
            "name",
            "description",
            "ratio",
            "screenshots",
            "featureEnabled",
            "featureValue",
            "featureValues",
        )

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["featureEnabled"] = False
        data["featureValue"] = ""

        if instance.feature_values.exists():
            feature_value = (
                instance.feature_values.all().order_by("feature_config__slug").first()
            )
            data["featureEnabled"] = feature_value.enabled
            data["featureValue"] = feature_value.value

        return data

    def validate_name(self, value):
        slug_name = slugify(value)
        if not slug_name:
            raise serializers.ValidationError(
                "Name needs to contain alphanumeric characters."
            )
        return value

    def validate(self, data):
        data = super().validate(data)

        feature_values = data.get("featureValues")

        if feature_values is not None:
            unique_features = set(fv["feature_config"] for fv in feature_values)
            if None not in unique_features and len(feature_values) != len(
                unique_features
            ):
                raise serializers.ValidationError(
                    {
                        "featureValues": [
                            {
                                "featureConfig": (
                                    NimbusConstants.ERROR_DUPLICATE_BRANCH_FEATURE_VALUE
                                )
                            }
                            for fv in feature_values
                        ]
                    }
                )

        return data

    def create(self, data):
        data["slug"] = slugify(data["name"])
        return super().create(data)

    def _save_feature_values(
        self, feature_enabled, feature_value, feature_values, branch
    ):
        feature_config = None
        if branch.experiment.feature_configs.exists():
            feature_config = (
                branch.experiment.feature_configs.all().order_by("slug").first()
            )

        branch.feature_values.all().delete()

        if branch.experiment.application != NimbusExperiment.Application.DESKTOP:
            feature_enabled = True

        if feature_value is not None:
            NimbusBranchFeatureValue.objects.create(
                branch=branch,
                feature_config=feature_config,
                enabled=feature_enabled,
                value=feature_value,
            )
        elif feature_values is not None:
            for feature_value_data in feature_values:
                NimbusBranchFeatureValue.objects.create(
                    branch=branch, **feature_value_data
                )

    def _save_screenshots(self, screenshots, branch):
        if screenshots is not None:
            updated_screenshots = dict(
                (x["id"], x) for x in screenshots if x.get("id", None)
            )
            for screenshot in branch.screenshots.all():
                screenshot_id = screenshot.id
                if screenshot_id not in updated_screenshots:
                    screenshot.delete()
                else:
                    serializer = NimbusBranchScreenshotSerializer(
                        screenshot,
                        data=updated_screenshots[screenshot_id],
                        partial=True,
                    )
                    if serializer.is_valid(raise_exception=True):
                        serializer.save()

            new_screenshots = (x for x in screenshots if not x.get("id", None))
            for screenshot_data in new_screenshots:
                serializer = NimbusBranchScreenshotSerializer(
                    data=screenshot_data, partial=True
                )
                if serializer.is_valid(raise_exception=True):
                    serializer.save(branch=branch)

    def save(self, *args, **kwargs):
        feature_enabled = self.validated_data.pop("featureEnabled", False)
        feature_value = self.validated_data.pop("featureValue", None)
        feature_values = self.validated_data.pop("featureValues", None)
        screenshots = self.validated_data.pop("screenshots", None)

        with transaction.atomic():
            branch = super().save(*args, **kwargs)

            self._save_feature_values(
                feature_enabled, feature_value, feature_values, branch
            )
            self._save_screenshots(screenshots, branch)

        return branch


class NimbusExperimentBranchMixin:
    def validate(self, data):
        data = super().validate(data)
        data = self._validate_duplicate_branch_names(data)
        return data

    def _validate_duplicate_branch_names(self, data):
        if "referenceBranch" in data and "treatmentBranches" in data:
            ref_branch_name = data["referenceBranch"]["name"]
            treatment_branch_names = [
                branch["name"] for branch in data["treatmentBranches"]
            ]
            all_names = [ref_branch_name, *treatment_branch_names]
            unique_names = set(all_names)

            if len(all_names) != len(unique_names):
                raise serializers.ValidationError(
                    {
                        "referenceBranch": {
                            "name": NimbusConstants.ERROR_DUPLICATE_BRANCH_NAME
                        },
                        "treatmentBranches": [
                            {"name": NimbusConstants.ERROR_DUPLICATE_BRANCH_NAME}
                            for i in data["treatmentBranches"]
                        ],
                    }
                )
        return data

    def update(self, experiment, data):
        data.pop("referenceBranch", None)
        data.pop("treatmentBranches", None)

        branches_data = []

        if (
            reference_branch_data := self.initial_data.get("referenceBranch")
        ) is not None:
            branches_data.append(reference_branch_data)

        if (
            treatment_branches_data := self.initial_data.get("treatmentBranches")
        ) is not None:
            branches_data.extend(treatment_branches_data)

        with transaction.atomic():
            experiment = super().update(experiment, data)

            if set(["referenceBranch", "treatmentBranches"]).intersection(
                set(self.initial_data.keys())
            ):
                saved_branch_ids = set(
                    experiment.branches.all().values_list("id", flat=True)
                )
                updated_branch_ids = set([b["id"] for b in branches_data if b.get("id")])
                deleted_branch_ids = saved_branch_ids - updated_branch_ids
                for deleted_branch_id in deleted_branch_ids:
                    NimbusBranch.objects.get(id=deleted_branch_id).delete()

            for branch_data in branches_data:
                branch = None
                if branch_data.get("id") is not None:
                    branch = NimbusBranch.objects.get(id=branch_data["id"])

                serializer = NimbusBranchSerializer(
                    instance=branch,
                    data=branch_data,
                    partial=True,
                )
                if serializer.is_valid(raise_exception=True):
                    branch = serializer.save(experiment=experiment)

                    if branch_data == reference_branch_data:
                        experiment.reference_branch = branch
                        experiment.save()

        return experiment


class NimbusDocumentationLinkSerializer(serializers.ModelSerializer):
    class Meta:
        model = NimbusDocumentationLink
        fields = (
            "title",
            "link",
        )


class NimbusExperimentDocumentationLinkMixin:
    def update(self, experiment, data):
        documentation_links_data = data.pop("documentationLinks", None)
        experiment = super().update(experiment, data)
        if documentation_links_data is not None:
            with transaction.atomic():
                experiment.documentation_links.all().delete()
                if documentation_links_data:
                    for link_data in documentation_links_data:
                        NimbusDocumentationLink.objects.create(
                            experiment=experiment, **link_data
                        )

        return experiment


class NimbusStatusValidationMixin:
    def validate(self, data):
        data = super().validate(data)

        restrictive_statuses = {
            "status": NimbusConstants.STATUS_ALLOWS_UPDATE,
            "publish_status": NimbusConstants.PUBLISH_STATUS_ALLOWS_UPDATE,
        }

        if self.instance:
            for status_field, restricted_statuses in restrictive_statuses.items():
                current_status = getattr(self.instance, status_field)
                is_locked = current_status not in restricted_statuses
                modifying_fields = set(data.keys()) - set(
                    NimbusExperiment.STATUS_UPDATE_EXEMPT_FIELDS
                )
                is_modifying_locked_fields = set(data.keys()).issubset(modifying_fields)
                if is_locked and is_modifying_locked_fields:
                    raise serializers.ValidationError(
                        {
                            "experiment": [
                                f"Nimbus Experiment has {status_field} "
                                f"'{current_status}', only "
                                f"{NimbusExperiment.STATUS_UPDATE_EXEMPT_FIELDS} "
                                f"can be changed, not: {modifying_fields}"
                            ]
                        }
                    )

            if (
                SiteFlag.objects.value(SiteFlagNameChoices.LAUNCHING_DISABLED)
                and self.instance.status == NimbusExperiment.Status.DRAFT
                and data.get("status_next") == NimbusExperiment.Status.LIVE
            ):
                raise serializers.ValidationError(
                    {"statusNext": NimbusExperiment.ERROR_LAUNCHING_DISABLED}
                )

        return data


class NimbusStatusTransitionValidator:
    requires_context = True

    def __init__(self, transitions):
        self.transitions = transitions

    def __call__(self, value, serializer_field):
        field_name = serializer_field.source_attrs[-1]
        instance = getattr(serializer_field.parent, "instance", None)

        if instance and value:
            instance_value = getattr(instance, field_name)

            if value != instance_value and value not in self.transitions.get(
                instance_value, ()
            ):
                raise serializers.ValidationError(
                    f"Nimbus Experiment {field_name} cannot transition "
                    f"from {instance_value} to {value}."
                )


class NimbusExperimentSerializer(
    NimbusExperimentBranchMixin,
    NimbusStatusValidationMixin,
    NimbusExperimentDocumentationLinkMixin,
    ExperimentNameValidatorMixin,
    serializers.ModelSerializer,
):
    name = serializers.CharField(
        min_length=1, max_length=80, required=False, allow_blank=True
    )
    slug = serializers.ReadOnlyField()
    application = serializers.ChoiceField(
        choices=NimbusExperiment.Application.choices, required=False
    )
    channel = serializers.ChoiceField(
        choices=NimbusExperiment.Channel.choices, required=False
    )
    publicDescription = serializers.CharField(
        source="public_description",
        min_length=0,
        max_length=1024,
        required=False,
        allow_blank=True,
    )
    isEnrollmentPaused = serializers.BooleanField(source="is_paused", required=False)
    riskMitigationLink = serializers.URLField(
        source="risk_mitigation_link",
        min_length=0,
        max_length=255,
        required=False,
        allow_blank=True,
    )
    documentationLinks = NimbusDocumentationLinkSerializer(many=True, required=False)
    hypothesis = serializers.CharField(
        min_length=0, max_length=1024, required=False, allow_blank=True
    )
    referenceBranch = NimbusBranchSerializer(required=False)
    treatmentBranches = NimbusBranchSerializer(many=True, required=False)
    featureConfig = serializers.PrimaryKeyRelatedField(
        queryset=NimbusFeatureConfig.objects.all(),
        allow_null=True,
        required=False,
        write_only=True,
    )
    featureConfigs = serializers.PrimaryKeyRelatedField(
        queryset=NimbusFeatureConfig.objects.all(),
        many=True,
        allow_null=True,
        required=False,
        write_only=True,
    )
    primaryOutcomes = serializers.ListField(
        source="primary_outcomes", child=serializers.CharField(), required=False
    )
    secondaryOutcomes = serializers.ListField(
        source="secondary_outcomes", child=serializers.CharField(), required=False
    )
    populationPercent = serializers.DecimalField(
        source="population_percent",
        max_digits=7,
        decimal_places=4,
        min_value=0.0,
        max_value=100.0,
        required=False,
    )
    status = serializers.ChoiceField(
        choices=NimbusExperiment.Status.choices,
        required=False,
        validators=[
            NimbusStatusTransitionValidator(
                transitions=NimbusConstants.VALID_STATUS_TRANSITIONS,
            )
        ],
    )
    publishStatus = serializers.ChoiceField(
        source="publish_status",
        choices=NimbusExperiment.PublishStatus.choices,
        required=False,
        validators=[
            NimbusStatusTransitionValidator(
                transitions=NimbusConstants.VALID_PUBLISH_STATUS_TRANSITIONS
            )
        ],
    )
    changelogMessage = serializers.CharField(
        min_length=0, max_length=1024, required=True, allow_blank=False
    )
    countries = serializers.PrimaryKeyRelatedField(
        queryset=Country.objects.all(),
        allow_null=True,
        required=False,
        many=True,
    )
    locales = serializers.PrimaryKeyRelatedField(
        queryset=Locale.objects.all(),
        allow_null=True,
        required=False,
        many=True,
    )
    languages = serializers.PrimaryKeyRelatedField(
        queryset=Language.objects.all(),
        allow_null=True,
        required=False,
        many=True,
    )
    conclusionRecommendation = serializers.ChoiceField(
        source="conclusion_recommendation",
        choices=NimbusExperiment.ConclusionRecommendation.choices,
        allow_null=True,
        required=False,
    )
    warnFeatureSchema = serializers.BooleanField(
        source="warn_feature_schema", required=False
    )
    firefoxMinVersion = serializers.ChoiceField(
        source="firefox_min_version",
        choices=NimbusExperiment.Version.choices,
        allow_null=True,
        required=False,
    )
    firefoxMaxVersion = serializers.ChoiceField(
        source="firefox_max_version",
        choices=NimbusExperiment.Version.choices,
        allow_null=True,
        required=False,
    )
    isRollout = serializers.BooleanField(
        source="is_rollout",
        required=False,
    )
    isArchived = serializers.BooleanField(
        source="is_archived",
        required=False,
    )
    isSticky = serializers.BooleanField(
        source="is_sticky",
        required=False,
    )
    isFirstRun = serializers.BooleanField(
        source="is_first_run",
        required=False,
    )
    proposedDuration = serializers.IntegerField(
        source="proposed_duration",
        required=False,
    )
    proposedEnrollment = serializers.IntegerField(
        source="proposed_enrollment",
        required=False,
    )
    riskBrand = serializers.BooleanField(
        source="risk_brand",
        required=False,
    )
    riskPartnerRelated = serializers.BooleanField(
        source="risk_partner_related",
        required=False,
    )
    riskRevenue = serializers.BooleanField(
        source="risk_revenue",
        required=False,
    )
    statusNext = serializers.ChoiceField(
        source="status_next",
        choices=NimbusExperiment.Status.choices,
        required=False,
        allow_null=True,
    )
    takeawaysSummary = serializers.CharField(
        source="takeaways_summary",
        required=False,
    )
    targetingConfigSlug = serializers.CharField(
        source="targeting_config_slug",
        required=False,
    )
    totalEnrolledClients = serializers.IntegerField(
        source="total_enrolled_clients",
        required=False,
    )

    class Meta:
        model = NimbusExperiment
        fields = [
            "application",
            "changelogMessage",
            "channel",
            "conclusionRecommendation",
            "countries",
            "documentationLinks",
            "featureConfig",
            "featureConfigs",
            "firefoxMaxVersion",
            "firefoxMinVersion",
            "hypothesis",
            "isArchived",
            "isEnrollmentPaused",
            "isFirstRun",
            "isRollout",
            "isSticky",
            "languages",
            "locales",
            "name",
            "populationPercent",
            "primaryOutcomes",
            "proposedDuration",
            "proposedEnrollment",
            "publicDescription",
            "publishStatus",
            "referenceBranch",
            "riskBrand",
            "riskMitigationLink",
            "riskPartnerRelated",
            "riskRevenue",
            "secondaryOutcomes",
            "slug",
            "status",
            "statusNext",
            "takeawaysSummary",
            "targetingConfigSlug",
            "totalEnrolledClients",
            "treatmentBranches",
            "warnFeatureSchema",
        ]

    def __init__(self, instance=None, data=None, **kwargs):
        self.should_call_preview_task = instance and (
            (
                instance.status == NimbusExperiment.Status.DRAFT
                and (data.get("status") == NimbusExperiment.Status.PREVIEW)
            )
            or (
                instance.status == NimbusExperiment.Status.PREVIEW
                and (data.get("status") == NimbusExperiment.Status.DRAFT)
            )
        )
        self.should_call_push_task = (
            data.get("publishStatus") == NimbusExperiment.PublishStatus.APPROVED
        )
        super().__init__(instance=instance, data=data, **kwargs)

    def validate_isArchived(self, is_archived):
        if self.instance.status not in (
            NimbusExperiment.Status.DRAFT,
            NimbusExperiment.Status.COMPLETE,
        ):
            raise serializers.ValidationError(
                f"An experiment in status {self.instance.status} can not be archived"
            )

        if self.instance.publish_status != NimbusExperiment.PublishStatus.IDLE:
            raise serializers.ValidationError(
                f"An experiment in publish status {self.instance.publish_status} "
                "can not be archived"
            )
        return is_archived

    def validate_publishStatus(self, publish_status):
        if publish_status == NimbusExperiment.PublishStatus.APPROVED and (
            self.instance.publish_status != NimbusExperiment.PublishStatus.IDLE
            and not self.instance.can_review(self.context["user"])
        ):
            raise serializers.ValidationError(
                f'{self.context["user"]} can not review this experiment.'
            )
        return publish_status

    def validate_hypothesis(self, hypothesis):
        if hypothesis.strip() == NimbusExperiment.HYPOTHESIS_DEFAULT.strip():
            raise serializers.ValidationError(
                "Please describe the hypothesis of your experiment."
            )
        return hypothesis

    def validate_primaryOutcomes(self, value):
        value_set = set(value)

        if len(value) > NimbusExperiment.MAX_PRIMARY_OUTCOMES:
            raise serializers.ValidationError(
                "Exceeded maximum primary outcome limit of "
                f"{NimbusExperiment.MAX_PRIMARY_OUTCOMES}."
            )

        valid_outcomes = set(
            [o.slug for o in Outcomes.by_application(self.instance.application)]
        )

        if valid_outcomes.intersection(value_set) != value_set:
            invalid_outcomes = value_set - valid_outcomes
            raise serializers.ValidationError(
                f"Invalid choices for primary outcomes: {invalid_outcomes}"
            )

        return value

    def validate_secondaryOutcomes(self, value):
        value_set = set(value)
        valid_outcomes = set(
            [o.slug for o in Outcomes.by_application(self.instance.application)]
        )

        if valid_outcomes.intersection(value_set) != value_set:
            invalid_outcomes = value_set - valid_outcomes
            raise serializers.ValidationError(
                f"Invalid choices for secondary outcomes: {invalid_outcomes}"
            )

        return value

    def validate_statusNext(self, value):
        valid_status_next = NimbusExperiment.VALID_STATUS_NEXT_VALUES.get(
            self.instance.status, ()
        )
        if value not in valid_status_next:
            choices_str = ", ".join(str(choice) for choice in valid_status_next)
            raise serializers.ValidationError(
                f"Invalid choice for status_next: '{value}' - with status "
                f"'{self.instance.status}', the only valid choices are "
                f"'{choices_str}'"
            )

        return value

    def validate(self, data):
        data = super().validate(data)

        non_is_archived_fields = set(data.keys()) - set(
            NimbusExperiment.ARCHIVE_UPDATE_EXEMPT_FIELDS
        )
        if self.instance and self.instance.is_archived and non_is_archived_fields:
            raise serializers.ValidationError(
                {
                    field: f"{field} can't be updated while an experiment is archived"
                    for field in non_is_archived_fields
                }
            )

        primary_outcomes = set(data.get("primary_outcomes", []))
        secondary_outcomes = set(data.get("secondary_outcomes", []))
        if primary_outcomes.intersection(secondary_outcomes):
            raise serializers.ValidationError(
                {
                    "primaryOutcomes": (
                        "Primary outcomes cannot overlap with secondary outcomes."
                    )
                }
            )

        if self.instance and "targeting_config_slug" in data:
            targeting_config_slug = data["targeting_config_slug"]
            application_choice = NimbusExperiment.Application(self.instance.application)
            targeting_config = NimbusExperiment.TARGETING_CONFIGS[targeting_config_slug]

            if application_choice.name not in targeting_config.application_choice_names:
                raise serializers.ValidationError(
                    {
                        "targetingConfigSlug": (
                            f"Targeting config '{targeting_config.name}' is not "
                            f"available for application '{application_choice.label}'"
                        )
                    }
                )

        proposed_enrollment = data.get("proposed_enrollment")
        proposed_duration = data.get("proposed_duration")
        if (
            None not in (proposed_enrollment, proposed_duration)
            and proposed_enrollment > proposed_duration
        ):
            raise serializers.ValidationError(
                {
                    "proposedEnrollment": (
                        "The enrollment duration must be less than or "
                        "equal to the experiment duration."
                    )
                }
            )

        return data

    def update(self, experiment, validated_data):
        self.changelog_message = validated_data.pop("changelogMessage")
        return super().update(experiment, validated_data)

    def create(self, validated_data):
        validated_data.update(
            {
                "slug": slugify(validated_data["name"]),
                "owner": self.context["user"],
                "channel": list(
                    NimbusExperiment.APPLICATION_CONFIGS[
                        validated_data["application"]
                    ].channel_app_id.keys()
                )[0],
            }
        )
        self.changelog_message = validated_data.pop("changelogMessage")
        return super().create(validated_data)

    def save(self):
        feature_config_provided = "featureConfig" in self.validated_data
        feature_config = self.validated_data.pop("featureConfig", None)
        feature_configs_provided = "featureConfigs" in self.validated_data
        feature_configs = self.validated_data.pop("featureConfigs", None)

        with transaction.atomic():
            # feature_configs must be set before we call super to make sure
            # the feature_config is available when the branches save their
            # feature_values
            if self.instance:
                if feature_config_provided:
                    self.instance.feature_configs.clear()

                if feature_config:
                    self.instance.feature_configs.add(feature_config)

                if feature_configs_provided:
                    self.instance.feature_configs.clear()

                if feature_configs:
                    self.instance.feature_configs.add(*feature_configs)

            experiment = super().save()

            if experiment.has_filter(experiment.Filters.SHOULD_ALLOCATE_BUCKETS):
                experiment.allocate_bucket_range()

            if self.should_call_preview_task:
                nimbus_synchronize_preview_experiments_in_kinto.apply_async(countdown=5)

            if self.should_call_push_task:
                collection = experiment.application_config.kinto_collection
                nimbus_check_kinto_push_queue_by_collection.apply_async(
                    countdown=5, args=[collection]
                )

            generate_nimbus_changelog(
                experiment, self.context["user"], message=self.changelog_message
            )

            return experiment


class NimbusExperimentCsvSerializer(serializers.ModelSerializer):
    experiment_name = serializers.CharField(source="name")
    product_area = serializers.CharField(source="application")
    rollout = serializers.BooleanField(source="is_rollout")
    owner = serializers.SlugRelatedField(read_only=True, slug_field="email")
    feature_configs = serializers.SerializerMethodField()
    experiment_summary = serializers.CharField(source="experiment_url")
    results_url = serializers.SerializerMethodField()

    class Meta:
        model = NimbusExperiment
        fields = [
            "launch_month",
            "product_area",
            "experiment_name",
            "owner",
            "feature_configs",
            "start_date",
            "enrollment_duration",
            "end_date",
            "results_url",
            "experiment_summary",
            "rollout",
            "hypothesis",
        ]

    def get_feature_configs(self, obj):
        sorted_features = sorted(
            obj.feature_configs.all(), key=lambda feature: feature.name
        )
        return ",".join([feature.name for feature in sorted_features])

    def get_results_url(self, obj):
        if obj.results_ready:
            return obj.experiment_url + "results"
        else:
            return ""


class NimbusBranchScreenshotReviewSerializer(NimbusBranchScreenshotSerializer):
    # Round-trip serialization & validation for review can use a string path
    image = serializers.CharField(required=True)
    description = serializers.CharField(required=True)


class NimbusBranchFeatureValueReviewSerializer(NimbusBranchFeatureValueSerializer):
    id = serializers.IntegerField()

    class Meta:
        model = NimbusBranchFeatureValue
        fields = (
            "id",
            "branch",
            "featureConfig",
            "enabled",
            "value",
        )

    def validate_value(self, value):
        if value:
            try:
                json.loads(value)
            except Exception as e:
                raise serializers.ValidationError(f"Invalid JSON: {e.msg}")
        return value

    def validate(self, data):
        feature_config = data.get("feature_config")
        value = data.get("value")
        branch = data.get("branch")

        # We can only run this validation for the multifeature case
        # otherwise it will prevent the single feature validation from running
        # so we check that there's more than 1 feature or return early
        # This check can be removed with #6744
        branch_feature_value_id = data.get("id")
        branch_feature_value = self.Meta.model.objects.get(id=branch_feature_value_id)
        if not branch_feature_value.branch.experiment.feature_configs.count() > 1:
            return data

        if all([branch, feature_config, value]) and feature_config.schema:
            json_value = json.loads(value)
            try:
                jsonschema.validate(json_value, json.loads(feature_config.schema))
            except jsonschema.ValidationError as e:
                if not branch.experiment.warn_feature_schema:
                    raise serializers.ValidationError({"value": e.message})

        return data


class NimbusBranchReviewSerializer(NimbusBranchSerializer):
    featureValues = NimbusBranchFeatureValueReviewSerializer(
        source="feature_values", many=True, required=True
    )
    screenshots = NimbusBranchScreenshotReviewSerializer(many=True, required=False)

    def validate_featureValue(self, value):
        if value:
            try:
                json.loads(value)
            except Exception as e:
                raise serializers.ValidationError(f"Invalid JSON: {e.msg}")
        return value

    def validate(self, data):
        if data.get("featureEnabled") and not data.get("featureValue"):
            raise serializers.ValidationError(
                {"featureValue": NimbusConstants.ERROR_BRANCH_NO_VALUE}
            )

        if data.get("featureValue") and not data.get("featureEnabled"):
            raise serializers.ValidationError(
                {"featureEnabled": NimbusConstants.ERROR_BRANCH_NO_ENABLED}
            )

        return data


class NimbusReviewSerializer(serializers.ModelSerializer):
    publicDescription = serializers.CharField(source="public_description", required=True)
    proposedDuration = serializers.IntegerField(
        source="proposed_duration", required=True, min_value=1
    )
    proposedEnrollment = serializers.IntegerField(
        source="proposed_enrollment", required=True, min_value=1
    )
    populationPercent = serializers.DecimalField(
        source="population_percent",
        max_digits=7,
        decimal_places=4,
        min_value=0.00009,
        max_value=100.0,
        required=True,
        error_messages={"min_value": NimbusConstants.ERROR_POPULATION_PERCENT_MIN},
    )
    totalEnrolledClients = serializers.IntegerField(
        source="total_enrolled_clients", required=True, min_value=1
    )
    firefoxMinVersion = serializers.ChoiceField(
        source="firefox_min_version",
        choices=NimbusExperiment.Version.choices,
        required=True,
    )
    firefoxMaxVersion = serializers.ChoiceField(
        source="firefox_max_version",
        choices=NimbusExperiment.Version.choices,
        required=True,
    )
    application = serializers.ChoiceField(
        NimbusExperiment.Application.choices, required=True
    )
    hypothesis = serializers.CharField(required=True)
    documentationLinks = NimbusDocumentationLinkSerializer(
        source="documentation_links", many=True
    )
    targetingConfigSlug = serializers.ChoiceField(
        source="targeting_config_slug",
        choices=NimbusExperiment.TargetingConfig.choices,
        required=True,
    )
    referenceBranch = NimbusBranchReviewSerializer(
        source="reference_branch", required=True
    )
    treatmentBranches = NimbusBranchReviewSerializer(
        source="treatment_branches", many=True
    )
    featureConfig = serializers.PrimaryKeyRelatedField(
        queryset=NimbusFeatureConfig.objects.all(),
        allow_null=False,
        error_messages={"null": NimbusConstants.ERROR_REQUIRED_FEATURE_CONFIG},
        write_only=True,
    )
    featureConfigs = serializers.PrimaryKeyRelatedField(
        source="feature_configs",
        queryset=NimbusFeatureConfig.objects.all(),
        many=True,
        allow_empty=False,
        error_messages={"empty": NimbusConstants.ERROR_REQUIRED_FEATURE_CONFIG},
    )
    primaryOutcomes = serializers.ListField(
        source="primary_outcomes", child=serializers.CharField(), required=False
    )
    secondaryOutcomes = serializers.ListField(
        source="secondary_outcomes", child=serializers.CharField(), required=False
    )
    riskPartnerRelated = serializers.BooleanField(
        source="risk_partner_related",
        required=True,
        allow_null=False,
        error_messages={"null": NimbusConstants.ERROR_REQUIRED_QUESTION},
    )
    riskRevenue = serializers.BooleanField(
        source="risk_revenue",
        required=True,
        allow_null=False,
        error_messages={"null": NimbusConstants.ERROR_REQUIRED_QUESTION},
    )
    riskBrand = serializers.BooleanField(
        source="risk_brand",
        required=True,
        allow_null=False,
        error_messages={"null": NimbusConstants.ERROR_REQUIRED_QUESTION},
    )
    isFirstRun = serializers.BooleanField(source="is_first_run")
    isRollout = serializers.BooleanField(source="is_rollout")
    isSticky = serializers.BooleanField(source="is_sticky")
    riskMitigationLink = serializers.URLField(
        source="risk_mitigation_link",
        min_length=0,
        max_length=255,
        required=False,
        allow_blank=True,
    )
    warnFeatureSchema = serializers.BooleanField(source="warn_feature_schema")

    class Meta:
        model = NimbusExperiment
        fields = [
            "application",
            "channel",
            "countries",
            "documentationLinks",
            "featureConfig",
            "featureConfigs",
            "firefoxMaxVersion",
            "firefoxMinVersion",
            "hypothesis",
            "isFirstRun",
            "isRollout",
            "isSticky",
            "languages",
            "locales",
            "name",
            "populationPercent",
            "primaryOutcomes",
            "proposedDuration",
            "proposedEnrollment",
            "publicDescription",
            "referenceBranch",
            "riskBrand",
            "riskMitigationLink",
            "riskPartnerRelated",
            "riskRevenue",
            "secondaryOutcomes",
            "targetingConfigSlug",
            "totalEnrolledClients",
            "treatmentBranches",
            "warnFeatureSchema",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.warnings = defaultdict(list)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["featureConfig"] = None
        if instance.feature_configs.exists():
            data["featureConfig"] = (
                instance.feature_configs.all().order_by("slug").first().id
            )
        return data

    def validate_referenceBranch(self, value):
        if value["description"] == "":
            raise serializers.ValidationError(
                {"description": [NimbusConstants.ERROR_REQUIRED_FIELD]}
            )
        return value

    def validate_treatmentBranches(self, value):
        errors = []

        for branch in value:
            error = {}
            if self.instance and self.instance.is_rollout:
                error["name"] = [NimbusConstants.ERROR_SINGLE_BRANCH_FOR_ROLLOUT]
            if branch["description"] == "":
                error["description"] = [NimbusConstants.ERROR_REQUIRED_FIELD]
            errors.append(error)

        if any(x for x in errors):
            raise serializers.ValidationError(errors)
        return value

    def validate_hypothesis(self, value):
        if value == NimbusExperiment.HYPOTHESIS_DEFAULT.strip():
            raise serializers.ValidationError("Hypothesis cannot be the default value.")
        return value

    def _validate_feature_value_against_schema(self, schema, value):
        json_value = json.loads(value)
        try:
            jsonschema.validate(json_value, schema, resolver=NestedRefResolver(schema))
        except jsonschema.ValidationError as exc:
            return [exc.message]

    def _validate_feature_configs(self, data):
        feature_configs = data.get("feature_configs", [])

        for feature_config in feature_configs:
            if self.instance.application != feature_config.application:
                raise serializers.ValidationError(
                    {
                        "featureConfigs": [
                            f"Feature Config application {feature_config.application} "
                            f"does not match experiment application "
                            f"{self.instance.application}."
                        ]
                    }
                )

        return data

    def _validate_feature_config(self, data):
        feature_config = data.get("featureConfig", None)
        warn_feature_schema = data.get("warn_feature_schema", False)

        if not feature_config or not feature_config.schema or not self.instance:
            return data

        if self.instance.application != feature_config.application:
            raise serializers.ValidationError(
                {
                    "featureConfig": [
                        f"Feature Config application {feature_config.application} does "
                        f"not match experiment application {self.instance.application}."
                    ]
                }
            )

        schema = json.loads(feature_config.schema)
        error_result = {}
        if data["reference_branch"].get("featureEnabled"):

            errors = self._validate_feature_value_against_schema(
                schema, data["reference_branch"]["featureValue"]
            )
            if errors:
                if warn_feature_schema:
                    self.warnings["referenceBranch"] = {"featureValue": errors}
                else:
                    error_result["referenceBranch"] = {"featureValue": errors}

        treatment_branches_errors = []
        treatment_branches_warnings = []
        for branch_data in data["treatment_branches"]:
            branch_error = None
            branch_warning = None

            if branch_data.get("featureEnabled", False):
                errors = self._validate_feature_value_against_schema(
                    schema, branch_data["featureValue"]
                )
                if errors:
                    if warn_feature_schema:
                        branch_warning = {"featureValue": errors}
                    else:
                        branch_error = {"featureValue": errors}

            treatment_branches_errors.append(branch_error)
            treatment_branches_warnings.append(branch_warning)

        if any(treatment_branches_warnings):
            self.warnings["treatmentBranches"] = treatment_branches_warnings

        if any(treatment_branches_errors):
            error_result["treatmentBranches"] = treatment_branches_errors

        if error_result:
            raise serializers.ValidationError(error_result)

        return data

    def _validate_versions(self, data):
        min_version = data.get("firefox_min_version", "")
        max_version = data.get("firefox_max_version", "")
        if (
            min_version != ""
            and max_version != ""
            and (
                NimbusExperiment.Version.parse(min_version)
                > NimbusExperiment.Version.parse(max_version)
            )
        ):
            raise serializers.ValidationError(
                {
                    "firefoxMinVersion": [NimbusExperiment.ERROR_FIREFOX_VERSION_MIN],
                    "firefoxMaxVersion": [NimbusExperiment.ERROR_FIREFOX_VERSION_MAX],
                }
            )
        return data

    def _validate_languages_versions(self, data):
        application = data.get("application")
        min_version = data.get("firefox_min_version", "")

        languages = data.get("languages", [])

        if languages:
            min_supported_version = (
                NimbusConstants.LANGUAGES_APPLICATION_SUPPORTED_VERSION[application]
            )

            if NimbusExperiment.Version.parse(
                min_version
            ) < NimbusExperiment.Version.parse(min_supported_version):

                raise serializers.ValidationError(
                    {
                        "languages": f"Language targeting is not \
                            supported for this application below \
                                version {min_supported_version}"
                    }
                )
        return data

    def _validate_countries_versions(self, data):
        application = data.get("application")
        min_version = data.get("firefox_min_version", "")

        countries = data.get("countries", [])

        if countries:

            min_supported_version = (
                NimbusConstants.COUNTRIES_APPLICATION_SUPPORTED_VERSION[application]
            )
            if NimbusExperiment.Version.parse(
                min_version
            ) < NimbusExperiment.Version.parse(min_supported_version):
                raise serializers.ValidationError(
                    {
                        "countries": f"Country targeting is \
                            not supported for this application \
                                below version {min_supported_version}"
                    }
                )
        return data

    def _validate_sticky_enrollment(self, data):
        targeting_config_slug = data.get("targeting_config_slug")
        targeting_config = NimbusExperiment.TARGETING_CONFIGS[targeting_config_slug]
        is_sticky = data.get("is_sticky")
        sticky_required = targeting_config.sticky_required
        if sticky_required and (not is_sticky):
            raise serializers.ValidationError(
                {
                    "isSticky": "Selected targeting expression requires sticky enrollment to function\
                    correctly"
                }
            )

        return data

    def _validate_feature_enabled_version(self, data):
        min_version = data.get("firefox_min_version", "")
        return NimbusExperiment.Version.parse(
            min_version
        ) >= NimbusExperiment.Version.parse(
            NimbusConstants.FEATURE_ENABLED_MIN_UNSUPPORTED_VERSION
        )

    def _validate_feature_enabled_for_treatment_branches(self, data):
        if self._validate_feature_enabled_version(data) and "treatment_branches" in data:
            treatment_branches_error = []
            for branch in data["treatment_branches"]:
                if not branch["featureEnabled"]:
                    treatment_branches_error.append(
                        {"featureEnabled": NimbusConstants.ERROR_FEATURE_ENABLED}
                    )

            if treatment_branches_error:
                raise serializers.ValidationError(
                    {"treatmentBranches": treatment_branches_error}
                )

        return data

    def _validate_feature_enabled_for_reference_branch(self, data):
        if (
            "reference_branch" in data
            and not data["reference_branch"]["featureEnabled"]
            and self._validate_feature_enabled_version(data)
        ):

            raise serializers.ValidationError(
                {
                    "referenceBranch": {
                        "featureEnabled": NimbusConstants.ERROR_FEATURE_ENABLED
                    },
                }
            )
        return data

    def _validate_rollout_version_support(self, data):
        if not self.instance or not self.instance.is_rollout:
            return data

        min_version = NimbusExperiment.Version.parse(self.instance.firefox_min_version)
        rollout_version_supported = NimbusExperiment.ROLLOUT_SUPPORT_VERSION.get(
            self.instance.application
        )
        if (
            rollout_version_supported is not None
            and min_version < NimbusExperiment.Version.parse(rollout_version_supported)
        ):
            raise serializers.ValidationError(
                {
                    "isRollout": f"Rollouts are not supported for this application \
                                below version {rollout_version_supported}"
                }
            )

        return data

    def validate(self, data):
        application = data.get("application")
        channel = data.get("channel")
        if application != NimbusExperiment.Application.DESKTOP and not channel:
            raise serializers.ValidationError(
                {"channel": "Channel is required for this application."}
            )
        data = super().validate(data)
        data = self._validate_feature_config(data)
        data = self._validate_feature_configs(data)
        data = self._validate_versions(data)
        data = self._validate_sticky_enrollment(data)
        data = self._validate_rollout_version_support(data)
        if application != NimbusExperiment.Application.DESKTOP:
            data = self._validate_languages_versions(data)
            data = self._validate_countries_versions(data)
        else:
            data = self._validate_feature_enabled_for_treatment_branches(data)
            data = self._validate_feature_enabled_for_reference_branch(data)
        return data


class NimbusExperimentCloneSerializer(
    ExperimentNameValidatorMixin, serializers.ModelSerializer
):
    parentSlug = serializers.SlugRelatedField(
        slug_field="slug", queryset=NimbusExperiment.objects.all()
    )
    name = serializers.CharField(min_length=1, max_length=80, required=True)
    rolloutBranchSlug = serializers.CharField(required=False, allow_null=True)

    class Meta:
        model = NimbusExperiment
        fields = (
            "parentSlug",
            "name",
            "rolloutBranchSlug",
        )

    def validate(self, data):
        data = super().validate(data)
        rollout_branch_slug = data.get("rolloutBranchSlug", None)
        if rollout_branch_slug:
            parent = data.get("parentSlug")
            if not parent.branches.filter(slug=rollout_branch_slug).exists():
                raise serializers.ValidationError(
                    {
                        "rolloutBranchSlug": (
                            f"Rollout branch {rollout_branch_slug} does not exist."
                        )
                    }
                )
        return data

    def save(self):
        parent = self.validated_data["parentSlug"]
        return parent.clone(
            self.validated_data["name"],
            self.context["user"],
            self.validated_data.get("rolloutBranchSlug", None),
        )
