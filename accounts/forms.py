from django import forms
from django.contrib.auth import authenticate
from .models import User
from .phone_numbers import normalize_phone_number


class SignupForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Password"})
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Confirm password"})
    )

    class Meta:
        model = User
        fields = ["name", "email", "mobile"]
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
            user = authenticate(username=clean_email, password=password)
            if user is None:
                user = authenticate(email=clean_email, password=password)
            if user is None:
                try:
                    user_obj = User.objects.get(email__iexact=clean_email)
                    if user_obj.check_password(password):
                        user = user_obj
                except User.DoesNotExist:
                    user = None

            self.user = user
            if self.user is None:
                raise forms.ValidationError("Invalid email or password.")
            if not self.user.is_active:
                raise forms.ValidationError("This account is disabled.")
        return cleaned_data
