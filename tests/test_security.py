import unittest

from app.security import create_auth_token, decrypt, encrypt, hash_password, mask_phone, phone_key, verify_auth_token, verify_password


class SecurityTests(unittest.TestCase):
    def test_encrypt_round_trip(self):
        value = "sensitive-session-value"
        encrypted = encrypt(value)
        self.assertNotEqual(encrypted, value)
        self.assertEqual(decrypt(encrypted), value)

    def test_phone_masking(self):
        masked = mask_phone("+905551234567")
        self.assertTrue(masked.startswith("+90"))
        self.assertTrue(masked.endswith("4567"))
        self.assertNotIn("123", masked)

    def test_phone_key_is_stable(self):
        self.assertEqual(phone_key("+90 555 123 45 67"), phone_key("+905551234567"))

    def test_password_hash(self):
        stored = hash_password("very-secure-password")
        self.assertTrue(verify_password("very-secure-password", stored))
        self.assertFalse(verify_password("wrong-password", stored))

    def test_auth_token(self):
        token = create_auth_token()
        self.assertTrue(verify_auth_token(token))
        self.assertFalse(verify_auth_token(token + "invalid"))


if __name__ == "__main__":
    unittest.main()
