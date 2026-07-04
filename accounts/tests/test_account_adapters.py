import datetime
from unittest.mock import MagicMock, patch

from django.conf import settings as django_settings

import pytest
from allauth.socialaccount.models import SocialLogin
from model_bakery import baker

from accounts.account_adapters import (
    AccountAdapter,
    HeadlessAdapter,
    SocialAccountAdapter,
)
from legal.factories import UserConsentFactory
from users.models import Profile, User


@pytest.mark.django_db
class TestSocialAccountAdapter:
    def test_get_connect_redirect_url(self, rf):
        adapter = SocialAccountAdapter()
        request = rf.get("/")
        with patch("accounts.account_adapters.reverse", return_value="/index/") as mock_reverse:
            url = adapter.get_connect_redirect_url(request, MagicMock())
            assert url == "/index/"
            mock_reverse.assert_called_with("index")

    def test_serialize_instance_user(self, user):
        adapter = SocialAccountAdapter()
        data = adapter.serialize_instance(user)
        assert data["id"] == user.id
        assert data["profile"]["first_name"] == user.profile.first_name

    def test_serialize_instance_user_without_profile(self):
        # Mirrors the pending-social-signup flow: allauth serializes a freshly
        # built, unsaved User that has no related Profile yet.
        adapter = SocialAccountAdapter()
        unsaved_user = User(email="nobody@example.com")

        data = adapter.serialize_instance(unsaved_user)

        assert data["email"] == "nobody@example.com"
        assert data["profile"] == {
            "first_name": "",
            "last_name": "",
            "profile_picture": None,
        }

    def test_serialize_instance_user_with_in_memory_profile(self):
        # deserialize_instance attaches an in-memory Profile; serialize must
        # round-trip its values rather than hit the database.
        adapter = SocialAccountAdapter()
        unsaved_user = User(email="nobody@example.com")
        unsaved_user.profile = Profile(user=unsaved_user, first_name="Ada", last_name="Lovelace")

        data = adapter.serialize_instance(unsaved_user)

        assert data["profile"]["first_name"] == "Ada"
        assert data["profile"]["last_name"] == "Lovelace"

    def test_populate_user_attaches_profile_from_provider_data(self):
        adapter = SocialAccountAdapter()
        sociallogin = MagicMock(spec=SocialLogin)
        sociallogin.user = User(email="grace@example.com")
        data = {"email": "grace@example.com", "first_name": "Grace", "last_name": "Hopper"}

        with patch.object(
            SocialAccountAdapter.__bases__[0],
            "populate_user",
            return_value=sociallogin.user,
        ):
            user = adapter.populate_user(None, sociallogin, data)

        assert isinstance(user.profile, Profile)
        assert user.profile.first_name == "Grace"
        assert user.profile.last_name == "Hopper"

    def test_save_user_creates_profile_for_auto_signup(self, db):
        # Auto-signup has no form; save_user must still leave a persisted Profile.
        adapter = SocialAccountAdapter()
        new_user = User(email="katherine@example.com")
        new_user.profile = Profile(user=new_user, first_name="Katherine", last_name="Johnson")
        sociallogin = MagicMock(spec=SocialLogin)
        sociallogin.user = new_user

        def _super_save(request, sociallogin, form=None):
            sociallogin.user.save()
            return sociallogin.user

        with patch.object(SocialAccountAdapter.__bases__[0], "save_user", side_effect=_super_save):
            user = adapter.save_user(None, sociallogin, form=None)

        persisted = Profile.objects.get(user=user)
        assert persisted.first_name == "Katherine"
        assert persisted.last_name == "Johnson"

    def test_save_user_enqueues_profile_picture_download(self, db):
        adapter = SocialAccountAdapter()
        new_user = User(email="margaret@example.com")
        sociallogin = MagicMock(spec=SocialLogin)
        sociallogin.user = new_user
        sociallogin.account = MagicMock(extra_data={"picture": "https://example.com/avatar.png"})

        def _super_save(request, sociallogin, form=None):
            sociallogin.user.save()
            return sociallogin.user

        with (
            patch.object(SocialAccountAdapter.__bases__[0], "save_user", side_effect=_super_save),
            patch("users.tasks.download_social_profile_picture.delay") as mock_delay,
        ):
            user = adapter.save_user(None, sociallogin, form=None)

        mock_delay.assert_called_once_with(user.pk, "https://example.com/avatar.png")

    def test_save_user_skips_picture_download_without_url(self, db):
        adapter = SocialAccountAdapter()
        new_user = User(email="annie@example.com")
        sociallogin = MagicMock(spec=SocialLogin)
        sociallogin.user = new_user
        sociallogin.account = MagicMock(extra_data={})

        def _super_save(request, sociallogin, form=None):
            sociallogin.user.save()
            return sociallogin.user

        with (
            patch.object(SocialAccountAdapter.__bases__[0], "save_user", side_effect=_super_save),
            patch("users.tasks.download_social_profile_picture.delay") as mock_delay,
        ):
            adapter.save_user(None, sociallogin, form=None)

        mock_delay.assert_not_called()

    def test_save_user_does_not_clobber_existing_profile(self, db):
        # Form-created profile (user-edited names) must win over provider names.
        adapter = SocialAccountAdapter()
        new_user = User(email="dorothy@example.com")
        sociallogin = MagicMock(spec=SocialLogin)
        sociallogin.user = new_user

        def _super_save(request, sociallogin, form=None):
            sociallogin.user.save()
            # Simulate the signup form having created the profile already.
            Profile.objects.create(user=sociallogin.user, first_name="Edited", last_name="ByForm")
            return sociallogin.user

        with patch.object(SocialAccountAdapter.__bases__[0], "save_user", side_effect=_super_save):
            user = adapter.save_user(None, sociallogin, form=MagicMock())

        persisted = Profile.objects.get(user=user)
        assert persisted.first_name == "Edited"
        assert persisted.last_name == "ByForm"

    def test_serialize_instance_sociallogin(self, user):
        adapter = SocialAccountAdapter()
        sociallogin = MagicMock(spec=SocialLogin)
        sociallogin.user = user
        with patch.object(
            SocialAccountAdapter.__bases__[0], "serialize_instance", return_value={"foo": "bar"}
        ):
            data = adapter.serialize_instance(sociallogin)
            assert data["foo"] == "bar"
            assert "user" in data

    def test_serialize_instance_fallback(self):
        adapter = SocialAccountAdapter()
        instance = object()
        with patch.object(
            SocialAccountAdapter.__bases__[0], "serialize_instance", return_value={"baz": 1}
        ):
            data = adapter.serialize_instance(instance)
            assert data == {"baz": 1}

    def test_deserialize_instance_sociallogin(self, user):
        adapter = SocialAccountAdapter()
        data = {"user": {"id": user.id}, "account": "foo"}
        with (
            patch.object(
                SocialAccountAdapter.__bases__[0], "deserialize_instance", return_value="fallback"
            ),
            patch(
                "allauth.socialaccount.models.SocialLogin.deserialize",
                return_value=MagicMock(spec=SocialLogin),
            ),
        ):
            result = adapter.deserialize_instance(SocialLogin, data)
            assert hasattr(result, "user")

    def test_deserialize_instance_user_with_id(self, user):
        adapter = SocialAccountAdapter()
        data = {"id": user.id, "profile": {}}
        result = adapter.deserialize_instance(User, data)
        assert result == user

    def test_deserialize_instance_user_without_id(self):
        adapter = SocialAccountAdapter()
        data = {"profile": {"first_name": "Bar", "last_name": "Baz"}}
        result = adapter.deserialize_instance(User, data)
        assert isinstance(result, User)
        assert isinstance(result.profile, Profile)
        assert result.profile.first_name == "Bar"

    def test_deserialize_instance_fallback(self):
        adapter = SocialAccountAdapter()
        data = {"foo": "bar"}
        with patch.object(
            SocialAccountAdapter.__bases__[0], "deserialize_instance", return_value="fallback"
        ):
            result = adapter.deserialize_instance(object, data)
            assert result == "fallback"


@pytest.mark.django_db
class TestAccountAdapter:
    @pytest.fixture
    def notification_service(self):
        return MagicMock()

    @pytest.fixture
    def adapter(self, notification_service):
        return AccountAdapter(notification_service=notification_service)

    def test_username_generation_disabled(self, adapter):
        # The User model has no username column; allauth must not try to
        # generate one (regression: "Unable to find a unique username" during
        # social signup). USER_MODEL_USERNAME_FIELD = None makes it a no-op.
        from allauth.account import app_settings as account_app_settings

        assert account_app_settings.USER_MODEL_USERNAME_FIELD is None
        # Must not raise NotImplementedError.
        adapter.populate_username(None, User(email="fresh@example.com"))

    def test_send_password_reset_mail(self, adapter, user):
        with (
            patch(
                "accounts.account_adapters.DefaultAccountAdapter.send_password_reset_mail"
            ) as super_send,
            patch("accounts.account_adapters.reverse", return_value="/reset/abc/"),
            patch(
                "accounts.account_adapters.build_absolute_uri",
                return_value="https://example.com/reset/abc/",
            ),
        ):
            adapter.send_password_reset_mail(user, user.email, {"key": "abc"})
            super_send.assert_called_once()
            adapter.notification_service.create_notification.assert_called_once()

    def test_send_confirmation_mail_signup(self, adapter):
        emailconfirmation = MagicMock()
        emailconfirmation.email_address.user_id = 1
        emailconfirmation.key = "key"
        with (
            patch.object(adapter, "get_email_confirmation_url", return_value="url"),
            patch("allauth.account.app_settings.EMAIL_VERIFICATION_BY_CODE_ENABLED", False),
        ):
            adapter.send_confirmation_mail(MagicMock(), emailconfirmation, signup=True)
            adapter.notification_service.create_notification.assert_called_once()

    def test_send_confirmation_mail_not_signup(self, adapter):
        emailconfirmation = MagicMock()
        emailconfirmation.email_address.user_id = 1
        emailconfirmation.key = "key"
        with (
            patch.object(adapter, "get_email_confirmation_url", return_value="url"),
            patch("allauth.account.app_settings.EMAIL_VERIFICATION_BY_CODE_ENABLED", True),
        ):
            adapter.send_confirmation_mail(MagicMock(), emailconfirmation, signup=False)
            adapter.notification_service.create_notification.assert_called_once()

    def test_send_mail(self, adapter):
        msg = MagicMock()
        with patch("accounts.account_adapters.DefaultAccountAdapter.render_mail", return_value=msg):
            with patch.object(django_settings, "SES_CONFIGURATION_SET", "foo"):
                adapter.send_mail("prefix", "email", {})
                assert msg.extra_headers["X-SES-CONFIGURATION-SET"] == "foo"
                msg.send.assert_called_once()

    def test_get_phone(self, adapter, user):
        user.phone_verified_date = datetime.datetime.now(datetime.UTC)
        user.phone_number = "+123456789"
        result = adapter.get_phone(user)
        assert result == ("+123456789", True)

    def test_get_phone_none(self, adapter, user):
        user.phone_verified_date = None
        user.phone_number = None
        result = adapter.get_phone(user)
        assert result == (None, False)

    def test_set_phone(self, adapter, user):
        adapter.set_phone(user, "+123456789", verified=True)
        assert user.phone_number == "+123456789"
        assert user.phone_verified_date is not None

    def test_set_phone_verified(self, adapter, user):
        adapter.set_phone_verified(user, "+123456789")
        assert user.phone_number == "+123456789"
        assert user.phone_verified_date is not None

    def test_get_user_by_phone_found(self, adapter, user):
        user.phone_number = "+123456789"
        user.save()
        found = adapter.get_user_by_phone("+123456789")
        assert found == user

    def test_get_user_by_phone_not_found(self, adapter):
        found = adapter.get_user_by_phone("+000000000")
        assert found is None

    def test_send_verification_code_sms_success(self, adapter, user):
        # The SMS consent gate (Phase 5, phone-keyed since Phase 8) requires a
        # recorded SMS_CONSENT UserConsent for the submitted phone, recorded
        # by this same user, before any verification SMS is dispatched
        # (BLOCKER 1 -- ties the gate to the requesting user's own consent
        # row); see accounts/tests/test_sms_consent_gate.py for the gate's
        # own tests.
        UserConsentFactory().create(user=user, phone_number="+123456789")

        with patch("accounts.account_adapters.logger.info") as log_info:
            adapter.send_verification_code_sms(user, "+123456789", "1234")
            log_info.assert_called()
            adapter.notification_service.create_notification.assert_called_once()

    def test_send_verification_code_sms_no_user(self, adapter):
        with patch("accounts.account_adapters.logger.warning") as log_warn:
            adapter.send_verification_code_sms(None, "+123456789", "1234")
            log_warn.assert_called_with("No user provided for sending verification code SMS.")

    def test_send_verification_code_sms_no_phone(self, adapter, user):
        with patch("accounts.account_adapters.logger.warning") as log_warn:
            adapter.send_verification_code_sms(user, None, "1234")
            log_warn.assert_called_with(
                "No phone number provided for sending verification code SMS."
            )

    def test_send_unknown_account_sms_is_a_no_op_even_with_a_consent_row(self, adapter):
        # BLOCKER 1 fix: the gate is tied to phone ownership
        # (`user__phone_number == phone`). `send_unknown_account_sms` only
        # ever fires when *no* user anywhere has `phone_number == phone`
        # (that's what makes the account "unknown"), so a consent row
        # recorded by an unrelated user (whose own phone differs) can never
        # satisfy the ownership join -- see
        # accounts/tests/test_sms_consent_gate.py for the gate's own tests.
        UserConsentFactory().create(user=baker.make(User), phone_number="+123456789")

        adapter.send_unknown_account_sms("+123456789")
        adapter.notification_service.create_one_off_notification.assert_not_called()

    def test_send_unknown_account_sms_no_phone(self, adapter):
        with patch("accounts.account_adapters.logger.warning") as log_warn:
            adapter.send_unknown_account_sms(None)
            log_warn.assert_called_with("No phone number provided for sending unknown account SMS.")


@pytest.mark.django_db
class TestHeadlessAdapter:
    def test_serialize_user(self, user):
        adapter = HeadlessAdapter()
        with patch("allauth.socialaccount.adapter.get_adapter") as get_adapter:
            get_adapter.return_value.serialize_instance.return_value = {"id": user.id}
            data = adapter.serialize_user(user)
            assert data["id"] == user.id
