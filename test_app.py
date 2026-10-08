import unittest
from unittest.mock import patch

import app


class MigrationChecks(unittest.TestCase):
    def test_requires_successful_migrate_instruction(self):
        response = {
            "result": {
                "meta": {
                    "err": None,
                    "logMessages": [
                        "Program log: Instruction: MigrateV2",
                    ],
                }
            }
        }
        with patch.object(app, "rpc", return_value=response):
            self.assertTrue(app.migration_confirmed("sig"))

    def test_rejects_pool_creation_without_migration(self):
        response = {
            "result": {
                "meta": {
                    "err": None,
                    "logMessages": [
                        "Program log: Instruction: CreatePool",
                    ],
                }
            }
        }
        with patch.object(app, "rpc", return_value=response):
            self.assertFalse(app.migration_confirmed("sig"))


class SwapParsing(unittest.TestCase):
    def test_transfer_buy_event(self):
        event = {
            "type": "SWAP", "source": "PUMP_AMM",
            "feePayer": "BUYER",
            "tokenTransfers": [
                {"mint": "Tokenpump", "fromUserAccount": "POOL",
                 "toUserAccount": "BUYER", "tokenAmount": 460301.799},
                {"mint": app.WSOL_MINT, "fromUserAccount": "BUYER",
                 "toUserAccount": "POOL", "tokenAmount": 0.034292614},
            ],
        }
        parsed = app.parse_pump_swap(event)
        self.assertEqual(parsed["side"], "buy")
        self.assertAlmostEqual(parsed["sol"], 0.034292614)

    def test_transfer_sell_event(self):
        event = {
            "type": "SWAP", "source": "PUMP_AMM",
            "feePayer": "SELLER",
            "tokenTransfers": [
                {"mint": "Tokenpump", "fromUserAccount": "SELLER",
                 "toUserAccount": "POOL", "tokenAmount": 100},
                {"mint": app.WSOL_MINT, "fromUserAccount": "POOL",
                 "toUserAccount": "SELLER", "tokenAmount": 0.5},
            ],
        }
        parsed = app.parse_pump_swap(event)
        self.assertEqual(parsed["side"], "sell")
        self.assertAlmostEqual(parsed["sol"], 0.5)

    def test_non_pumpswap_source_is_rejected(self):
        event = {
            "type": "SWAP", "source": "JUPITER", "feePayer": "BUYER",
            "tokenTransfers": [{"mint": "Tokenpump",
                                 "toUserAccount": "BUYER"}],
        }
        self.assertIsNone(app.parse_pump_swap(event))

    def test_ambiguous_swap_is_rejected(self):
        event = {
            "type": "SWAP", "source": "PUMP_AMM",
            "tokenTransfers": [{"mint": "Tokenpump"}],
        }
        self.assertIsNone(app.parse_pump_swap(event))


class WebhookAuth(unittest.TestCase):
    def test_empty_auth_allows_local_compatibility(self):
        class Request:
            headers = {}
        with patch.object(app, "WEBHOOK_AUTH", ""):
            self.assertTrue(app.webhook_authorized(Request()))

    def test_configured_auth_requires_exact_header(self):
        class Request:
            def __init__(self, value):
                self.headers = {"authorization": value}
        with patch.object(app, "WEBHOOK_AUTH", "secret"):
            self.assertTrue(app.webhook_authorized(Request("secret")))
            self.assertFalse(app.webhook_authorized(Request("wrong")))

    def test_rejects_failed_migration_transaction(self):
        response = {
            "result": {
                "meta": {
                    "err": {"InstructionError": [0, "Custom"]},
                    "logMessages": [
                        "Program log: Instruction: Migrate",
                    ],
                }
            }
        }
        with patch.object(app, "rpc", return_value=response):
            self.assertFalse(app.migration_confirmed("sig"))


if __name__ == "__main__":
    unittest.main()
