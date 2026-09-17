from __future__ import annotations

import unittest

from common.config import RuntimeConfig
from common.errors import ServiceError
from common.validation import identifier
from services.getmyinvoices.provider import GmiProvider
from services.lexware.provider import LexwareProvider, _normalize_voucher


class ProviderTests(unittest.TestCase):
    def test_getmyinvoices_normalization_does_not_invent_amounts(self) -> None:
        normalized = GmiProvider.normalize_document({"id": "doc-1", "supplierName": "Beispiel GmbH"})
        self.assertEqual(normalized["document_id"], "doc-1")
        self.assertIsNone(normalized["net_amount"])
        self.assertIsNone(normalized["tax_amount"])
        self.assertTrue(normalized["raw_fields_omitted"])

    def test_lexware_normalization_preserves_zero_amounts(self) -> None:
        normalized = _normalize_voucher({"id": "v-1", "organizationId": "org-1", "totalGrossAmount": 0, "totalTaxAmount": 0})
        self.assertEqual(normalized["gross_amount"], 0)
        self.assertEqual(normalized["tax_amount"], 0)

    def test_lexware_scope_violation_is_rejected(self) -> None:
        runtime = RuntimeConfig("secret", 5, 1024, 25, 31, 60, frozenset())
        provider = LexwareProvider(runtime, None, "org-a", None, None)  # type: ignore[arg-type]
        with self.assertRaises(ServiceError) as caught:
            provider._assert_scope({"organizationId": "org-b"}, required_field=True)
        self.assertEqual(caught.exception.error_code, "ORGANIZATION_SCOPE_VIOLATION")

    def test_identifier_allows_uuid_and_rejects_slash(self) -> None:
        self.assertEqual(identifier("aa93e8a8-2aa3-470b-b914-caad8a255dd8", "id"), "aa93e8a8-2aa3-470b-b914-caad8a255dd8")
        with self.assertRaises(ServiceError):
            identifier("id/other", "id")


if __name__ == "__main__":
    unittest.main()

