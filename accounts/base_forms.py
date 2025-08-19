from django import forms

from users.models import Profile


class BaseVintaScheduleSignupForm(forms.Form):
    """
    Base form for user signup.
    This form can be extended to include additional fields as needed.
    """

    first_name = forms.CharField(max_length=255, required=True, label="First Name")
    last_name = forms.CharField(max_length=255, required=True, label="Last Name")

    def signup(self, request, user):
        """
        Method to handle user signup.
        This method should be overridden in subclasses to implement custom signup logic.
        """
        user.save()

        first_name = self.cleaned_data.get("first_name", "")
        last_name = self.cleaned_data.get("last_name", "")

        try:
            profile = user.profile
            profile.first_name = first_name
            profile.last_name = last_name
            profile.save()
        except Profile.DoesNotExist:
            Profile.objects.create(
                user=user,
                first_name=first_name,
                last_name=last_name,
            )
        return user
