import os
import unittest
from pathlib import Path
from unittest.mock import patch

from mikrotik_reporting.config import load_mail_config


class ConfigTests(unittest.TestCase):
    def test_mail_notifier_path_is_explicit(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MIKROTIK_REPORT_TO": "recipient@example.net",
                "MIKROTIK_REPORT_NOTIFIER": "/opt/mail-notifier/send-mail.sh",
            },
            clear=True,
        ):
            config = load_mail_config()

        self.assertEqual(
            config.notifier,
            Path("/opt/mail-notifier/send-mail.sh"),
        )

    def test_mail_notifier_path_must_be_absolute(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "MIKROTIK_REPORT_TO": "recipient@example.net",
                    "MIKROTIK_REPORT_NOTIFIER": "mail-notifier/send-mail.sh",
                },
                clear=True,
            ),
            self.assertRaisesRegex(
                ValueError,
                "MIKROTIK_REPORT_NOTIFIER must be an absolute path",
            ),
        ):
            load_mail_config()


if __name__ == "__main__":
    unittest.main()
