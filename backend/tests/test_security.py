import hashlib
import hmac
import io
import unittest

from cre_security import (
    SlidingWindowRateLimiter,
    UploadRejected,
    hash_password,
    require_record_owner,
    safe_update,
    secure_save_upload,
    validate_upload_bytes,
    verify_password,
    verify_stripe_signature,
)


class Upload(io.BytesIO):
    def __init__(self, data, name):
        super().__init__(data)
        self.name = name

    def getbuffer(self):
        return super().getbuffer()


class SecurityTests(unittest.TestCase):
    def test_server_script_and_double_extension_uploads_are_blocked(self):
        for name in ("shell.php", "clip.mp4.jsp", "image.jpg.php"):
            with self.assertRaises(UploadRejected):
                validate_upload_bytes(b"not executable", name, "media")

    def test_extension_must_match_content_signature(self):
        with self.assertRaises(UploadRejected):
            validate_upload_bytes(b"<?php echo 1; ?>", "clip.mp4", "video")
        self.assertEqual(validate_upload_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 20, "clip.mp4", "video"), "clip.mp4")

    def test_secure_save_uses_content_hash_and_quarantine(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            upload = Upload(b"\x89PNG\r\n\x1a\n" + b"x" * 32, "../Poster Final.PNG")
            target = secure_save_upload(upload, root / "images", "image")
            self.assertEqual(target.parent, (root / "images").resolve())
            self.assertTrue(target.name.startswith("Poster Final-") and target.suffix == ".png")
            self.assertTrue(target.read_bytes().startswith(b"\x89PNG"))
            self.assertFalse(list((root / ".quarantine").glob("*")))

    def test_payment_signature_checks_timestamp_and_digest(self):
        payload = b'{"id":"evt_1"}'
        timestamp = 1_700_000_000
        digest = hmac.new(b"whsec_test", str(timestamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
        header = f"t={timestamp},v1={digest}"
        self.assertTrue(verify_stripe_signature(payload, header, "whsec_test", now=timestamp))
        self.assertFalse(verify_stripe_signature(payload, header, "wrong", now=timestamp))
        self.assertFalse(verify_stripe_signature(payload, header, "whsec_test", now=timestamp + 301))

    def test_passwords_mass_assignment_ownership_and_rate_limits_fail_closed(self):
        encoded = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", encoded))
        self.assertFalse(verify_password("wrong password", encoded))
        self.assertEqual(safe_update({"display_name": "A"}, {"display_name"}), {"display_name": "A"})
        with self.assertRaises(ValueError):
            safe_update({"display_name": "A", "is_admin": True}, {"display_name"})
        require_record_owner("user-1", "user-1")
        with self.assertRaises(PermissionError):
            require_record_owner("user-1", "user-2")
        limiter = SlidingWindowRateLimiter(2, 60)
        self.assertTrue(limiter.allow("login", now=0))
        self.assertTrue(limiter.allow("login", now=1))
        self.assertFalse(limiter.allow("login", now=2))
        self.assertTrue(limiter.allow("login", now=61))


if __name__ == "__main__":
    unittest.main()
