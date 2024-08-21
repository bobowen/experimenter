from django import forms
from django.contrib.auth.models import User
from django.http import HttpRequest
from django.utils.text import slugify

from experimenter.projects.models import Project
from experimenter.outcomes import Outcomes
from experimenter.targeting.constants import NimbusTargetingConfig, TargetingConstants
from experimenter.experiments.changelog_utils import generate_nimbus_changelog
from experimenter.experiments.models import NimbusExperiment, NimbusFeatureConfig
from experimenter.nimbus_ui_new.constants import NimbusUIConstants


class NimbusChangeLogFormMixin:
    def __init__(self, *args, request: HttpRequest = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request

    def get_changelog_message(self) -> str:
        raise NotImplementedError

    def save(self, *args, **kwargs):
        experiment = super().save(*args, **kwargs)
        generate_nimbus_changelog(
            experiment, self.request.user, self.get_changelog_message()
        )
        return experiment


class NimbusExperimentCreateForm(NimbusChangeLogFormMixin, forms.ModelForm):
    owner = forms.ModelChoiceField(
        User.objects.all(),
        widget=forms.widgets.HiddenInput(),
    )
    name = forms.CharField(
        label="",
        widget=forms.widgets.TextInput(
            attrs={
                "placeholder": "Public Name",
            }
        ),
    )
    slug = forms.CharField(
        required=False,
        widget=forms.widgets.HiddenInput(),
    )
    hypothesis = forms.CharField(
        label="",
        widget=forms.widgets.Textarea(),
        initial=NimbusUIConstants.HYPOTHESIS_PLACEHOLDER,
    )
    application = forms.ChoiceField(
        label="",
        choices=NimbusExperiment.Application.choices,
        widget=forms.widgets.Select(
            attrs={
                "class": "form-select",
            },
        ),
    )

    class Meta:
        model = NimbusExperiment
        fields = [
            "owner",
            "name",
            "slug",
            "hypothesis",
            "application",
        ]

    def get_changelog_message(self):
        return f'{self.request.user} created {self.cleaned_data["name"]}'

    def clean_name(self):
        name = self.cleaned_data["name"]
        slug = slugify(name)
        if not slug:
            raise forms.ValidationError(NimbusUIConstants.ERROR_NAME_INVALID)
        if NimbusExperiment.objects.filter(slug=slug).exists():
            raise forms.ValidationError(NimbusUIConstants.ERROR_SLUG_DUPLICATE)
        return name

    def clean_hypothesis(self):
        hypothesis = self.cleaned_data["hypothesis"]
        if hypothesis.strip() == NimbusUIConstants.HYPOTHESIS_PLACEHOLDER.strip():
            raise forms.ValidationError(NimbusUIConstants.ERROR_HYPOTHESIS_PLACEHOLDER)
        return hypothesis

    def clean(self):
        cleaned_data = super().clean()
        if "name" in cleaned_data:
            cleaned_data["slug"] = slugify(cleaned_data["name"])
        return cleaned_data


class NimbusOverviewForm(NimbusChangeLogFormMixin, forms.ModelForm):
    owner = forms.ModelChoiceField(
        queryset=User.objects.all().order_by("email"),
        widget=forms.widgets.Select(
            attrs={
                "class": "selectpicker form-control border",
                "data-live-search": "true",
            }
        ),
    )
    name = forms.CharField(
        widget=forms.widgets.TextInput(attrs={"class": "form-control"})
    )
    public_description = forms.CharField(
        widget=forms.widgets.Textarea(attrs={"class": "form-control"})
    )
    hypothesis = forms.CharField(
        widget=forms.widgets.Textarea(attrs={"class": "form-control"})
    )
    primary_outcomes = forms.MultipleChoiceField(
        choices=Outcomes.all(),
        widget=forms.widgets.SelectMultiple(
            attrs={
                "class": "selectpicker form-control border",
                "data-live-search": "true",
            }
        ),
    )
    secondary_outcomes = forms.MultipleChoiceField(
        choices=Outcomes.all(),
        widget=forms.widgets.SelectMultiple(
            attrs={
                "class": "selectpicker form-control border",
                "data-live-search": "true",
            }
        ),
    )
    projects = forms.ModelMultipleChoiceField(
        queryset=Project.objects.all(),
        widget=forms.widgets.SelectMultiple(
            attrs={
                "class": "selectpicker form-control border",
                "data-live-search": "true",
            }
        ),
    )
    subscribers = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(),
        widget=forms.widgets.SelectMultiple(
            attrs={
                "class": "selectpicker form-control border",
                "data-live-search": "true",
            }
        ),
    )

    class Meta:
        model = NimbusExperiment
        fields = [
            "owner",
            "name",
            "public_description",
            "hypothesis",
            "primary_outcomes",
            "secondary_outcomes",
            "projects",
            "subscribers",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance:
            application_outcome_choices = sorted(
                (o.slug, o.friendly_name)
                for o in Outcomes.by_application(self.instance.application)
            )
            self.fields["primary_outcomes"].choices = application_outcome_choices
            self.fields["secondary_outcomes"].choices = application_outcome_choices

    def get_changelog_message(self) -> str:
        return f"{self.request.user} updated overview"


class QAStatusForm(NimbusChangeLogFormMixin, forms.ModelForm):
    class Meta:
        model = NimbusExperiment
        fields = ["qa_status", "qa_comment"]
        widgets = {
            "qa_status": forms.Select(choices=NimbusExperiment.QAStatus),
        }

    def get_changelog_message(self):
        return f"{self.request.user} updated QA"


class TakeawaysForm(NimbusChangeLogFormMixin, forms.ModelForm):
    conclusion_recommendations = forms.MultipleChoiceField(
        choices=NimbusExperiment.ConclusionRecommendation.choices,
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )

    class Meta:
        model = NimbusExperiment
        fields = [
            "takeaways_metric_gain",
            "takeaways_gain_amount",
            "takeaways_qbr_learning",
            "takeaways_summary",
            "conclusion_recommendations",
        ]

    def get_changelog_message(self):
        return f"{self.request.user} updated takeaways"
