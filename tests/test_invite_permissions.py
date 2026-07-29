import unittest
from types import SimpleNamespace

from app.telegram_service import _entity_can_invite_users


class InvitePermissionTests(unittest.TestCase):
    @staticmethod
    def megagroup(**overrides):
        values = {
            "creator": False,
            "admin_rights": None,
            "megagroup": True,
            "left": False,
            "kicked": False,
            "deactivated": False,
            "banned_rights": None,
            "default_banned_rights": SimpleNamespace(invite_users=False),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_standard_member_can_invite_when_group_permission_is_open(self):
        self.assertTrue(_entity_can_invite_users(self.megagroup()))

    def test_standard_member_is_rejected_when_group_permission_is_closed(self):
        entity = self.megagroup(
            default_banned_rights=SimpleNamespace(invite_users=True)
        )
        self.assertFalse(_entity_can_invite_users(entity))

    def test_personal_restriction_overrides_open_group_permission(self):
        entity = self.megagroup(
            banned_rights=SimpleNamespace(invite_users=True)
        )
        self.assertFalse(_entity_can_invite_users(entity))

    def test_session_must_be_a_group_member(self):
        self.assertFalse(_entity_can_invite_users(self.megagroup(left=True)))

    def test_admin_permission_remains_supported(self):
        entity = self.megagroup(
            admin_rights=SimpleNamespace(invite_users=True),
            default_banned_rights=SimpleNamespace(invite_users=True),
        )
        self.assertTrue(_entity_can_invite_users(entity))

    def test_standard_member_permission_does_not_apply_to_broadcast_channel(self):
        entity = self.megagroup(megagroup=False)
        self.assertFalse(_entity_can_invite_users(entity))


if __name__ == "__main__":
    unittest.main()
