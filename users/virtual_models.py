import django_virtual_models as v

from users.models import Profile, User


class ProfileVirtualModel(v.VirtualModel):
    class Meta(v.VirtualModel.Meta):
        model = Profile


class UserVirtualModel(v.VirtualModel):
    profile = ProfileVirtualModel()

    class Meta(v.VirtualModel.Meta):
        model = User
