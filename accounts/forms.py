from django import forms
from django.contrib.auth import authenticate
import phonenumbers
from .models import User
from .phone_numbers import normalize_phone_number
from .login_security import BLOCK_MESSAGE, register_password_failure, reset_password_failures
from .request_context import get_current_request


class SignupForm(forms.ModelForm):
    country_code = forms.ChoiceField(
        choices=sorted(
            ((region, f"{region} +{phonenumbers.country_code_for_region(region)}")
             for region in phonenumbers.SUPPORTED_REGIONS),
            key=lambda item: item[0],
        ),
        initial="US",
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Password"})
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Confirm password"})
    )

    class Meta:
        model = User
        fields = ["name", "email", "country_code", "mobile"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Full name"}),
            "email": forms.EmailInput(attrs={"placeholder": "Email address"}),
            "mobile": forms.TextInput(attrs={"placeholder": "Mobile number"}),
        }

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_mobile(self):
        mobile = normalize_phone_number(
            self.cleaned_data["mobile"],
            self.data.get("country_code"),
        )
        if User.objects.filter(mobile=mobile).exists():
            raise forms.ValidationError("This mobile number is already registered.")
        return mobile

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")
        if password and confirm_password and password != confirm_password:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
        return user


class LoginForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"placeholder": "Email address"})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Password"})
    )

    def clean(self):
        cleaned_data = super().clean()
        email = cleaned_data.get("email")
        password = cleaned_data.get("password")

        if email and password:
            clean_email = email.strip().lower()
            candidate = User.objects.select_related("client").filter(email__iexact=clean_email).first()

            # Client accounts blocked by the five-attempt rule never reach
            # authentication until an administrator explicitly unblocks them.
            if candidate and not candidate.is_staff and not candidate.is_superuser and candidate.login_blocked_at:
                self.user = None
                raise forms.ValidationError(BLOCK_MESSAGE)

            user = authenticate(username=clean_email, password=password)
            if user is None:
                user = authenticate(email=clean_email, password=password)
            if user is None and candidate and candidate.check_password(password):
                user = candidate

            self.user = user
            if self.user is None:
                if candidate and not candidate.is_staff and not candidate.is_superuser:
                    blocked, attempts = register_password_failure(candidate, get_current_request())
                    if blocked:
                        raise forms.ValidationError(BLOCK_MESSAGE)
                    remaining = max(0, 5 - attempts)
                    raise forms.ValidationError(
                        f"Invalid email or password. {remaining} attempt(s) remaining before the account is blocked."
                    )
                raise forms.ValidationError("Invalid email or password.")
            if not self.user.is_active:
                raise forms.ValidationError("This account is disabled.")

            # A correct password clears the running failure counter. A blocked
            # account cannot get here because it must first be admin-unblocked.
            if not self.user.is_staff and not self.user.is_superuser:
                reset_password_failures(self.user)
        return cleaned_data
