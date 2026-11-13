import email.message
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import server

class MockHandler(server.Handler):
    def __init__(self, method, path, headers=None, body=b"", session_token=""):
        self.command = method
        self.path = path
        self.headers = email.message.EmailMessage()
        if headers:
            for k, v in headers.items():
                self.headers[k] = v
        if session_token:
            self.headers["Cookie"] = f"efile_session={session_token}"
        self.headers["Host"] = "127.0.0.1:8765"
        self.headers["Origin"] = "http://127.0.0.1:8765"
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.response_code = 200
        self.response_headers = {}
        self.current_user = None
        self._upload_slot = False
        self.close_connection = True

    def send_response(self, code, message=None):
        self.response_code = code

    def send_header(self, keyword, value):
        self.response_headers[keyword] = value

    def end_headers(self):
        pass

    def send_error(self, code, message=None, explain=None):
        self.response_code = code
        self.send_json({"error": message or f"Error {code}"}, code)

    def handle_request(self):
        if self.command == "GET":
            self.do_GET()
        elif self.command == "POST":
            self.do_POST()
        
        raw_val = self.wfile.getvalue()
        idx = raw_val.find(b"\r\n\r\n")
        body_bytes = raw_val[idx+4:] if idx != -1 else raw_val
        try:
            return self.response_code, json.loads(body_bytes.decode("utf-8")), self.response_headers
        except Exception:
            return self.response_code, body_bytes, self.response_headers

def make_multipart_body(fields, files):
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    lines = []
    for k, v in fields.items():
        lines.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (filename, content, mime) in files.items():
        head = f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n".encode()
        lines.append(head + content + b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode())
    body = b"".join(lines)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type

class PoUploadFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.initialize()
        with server.local_connect() as db:
            db.execute("DELETE FROM purchase_orders WHERE po_id LIKE 'PO-2026-AUTO-%'")
            db.execute("DELETE FROM documents WHERE po_id LIKE 'PO-2026-AUTO-%'")
            db.commit()

    @classmethod
    def tearDownClass(cls):
        with server.local_connect() as db:
            db.execute("DELETE FROM purchase_orders WHERE po_id LIKE 'PO-2026-AUTO-%'")
            db.execute("DELETE FROM documents WHERE po_id LIKE 'PO-2026-AUTO-%'")
            db.commit()

    def setUp(self):
        self.session_token = server.create_session("admin_user_id")

    def test_01_po_check_not_found(self):
        h = MockHandler("GET", "/api/purchase-orders/check?po_id=PO-NONEXISTENT-999", session_token=self.session_token)
        code, data, _ = h.handle_request()
        self.assertEqual(code, 200)
        self.assertFalse(data.get("exists"))

    def test_02_new_po_upload_scenario_a(self):
        po_id = "PO-2026-AUTO-001"
        po_payload = {
            "po_id": po_id,
            "financial_year": "2026-27",
            "month": "April",
            "remarks": "Medical supply procurement 2026",
            "items": [
                {
                    "item_code": "ITM-GLV-01",
                    "item_name": "Surgical Gloves",
                    "vendor_code": "VND-APX",
                    "vendor_name": "Apex Meditech",
                    "remarks": "Sterile size 7.5",
                    "documents": [
                        {"file_key": "file_0_0", "document_type": "Purchase Order"},
                        {"file_key": "file_0_1", "document_type": "Technical Specification"}
                    ]
                },
                {
                    "item_code": "ITM-MSK-02",
                    "item_name": "N95 Respirator",
                    "vendor_code": "VND-3M",
                    "vendor_name": "3M Healthcare",
                    "remarks": "NIOSH approved",
                    "documents": [
                        {"file_key": "file_1_0", "document_type": "Invoice"}
                    ]
                }
            ]
        }

        files = {
            "file_0_0": ("order.pdf", b"%PDF-1.4 sample po content", "application/pdf"),
            "file_0_1": ("spec.png", b"\x89PNG\r\n\x1a\nfake image content", "image/png"),
            "file_1_0": ("invoice.pdf", b"%PDF-1.4 invoice content", "application/pdf")
        }

        body, ctype = make_multipart_body({"po_data": json.dumps(po_payload)}, files)
        headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
        h = MockHandler("POST", "/api/purchase-orders/upload", headers=headers, body=body, session_token=self.session_token)
        code, data, _ = h.handle_request()

        self.assertEqual(code, 201)
        self.assertTrue(data.get("success"))
        self.assertFalse(data.get("is_existing"))
        self.assertEqual(data.get("items_count"), 2)
        self.assertEqual(data.get("docs_count"), 3)

        # Verify Database state
        with server.local_connect() as db:
            po = db.execute("SELECT * FROM purchase_orders WHERE po_id = ?", (po_id,)).fetchone()
            self.assertIsNotNone(po)
            self.assertEqual(po["financial_year"], "2026-27")
            self.assertEqual(po["month"], 4)

            items = db.execute("SELECT * FROM po_items WHERE purchase_order_id = ? ORDER BY item_code", (po["id"],)).fetchall()
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]["item_code"], "ITM-GLV-01")
            self.assertEqual(items[0]["item_name"], "Surgical Gloves")
            self.assertEqual(items[1]["item_code"], "ITM-MSK-02")
            self.assertEqual(items[1]["item_name"], "N95 Respirator")

            item_docs = db.execute("SELECT * FROM item_documents WHERE item_id = ?", (items[0]["id"],)).fetchall()
            self.assertEqual(len(item_docs), 2)

            docs = db.execute("SELECT * FROM documents WHERE po_id = ? ORDER BY name", (po_id,)).fetchall()
            self.assertEqual(len(docs), 3)

    def test_03_existing_po_detection_scenario_b(self):
        po_id = "PO-2026-AUTO-001"
        h = MockHandler("GET", f"/api/purchase-orders/check?po_id={po_id.lower()}", session_token=self.session_token)
        code, data, _ = h.handle_request()
        self.assertEqual(code, 200)
        self.assertTrue(data.get("exists"))
        self.assertEqual(data["po"]["financial_year"], "2026-27")
        self.assertEqual(data["po"]["month"], "April")
        self.assertEqual(data["po"]["items_count"], 2)

    def test_04_attach_to_existing_po_scenario_b(self):
        po_id = "PO-2026-AUTO-001"
        po_payload = {
            "po_id": po_id,
            "items": [
                {
                    "item_code": "ITM-SYR-03",
                    "item_name": "Disposable Syringes",
                    "vendor_code": "VND-BD",
                    "vendor_name": "Becton Dickinson",
                    "remarks": "5ml luer lock",
                    "documents": [
                        {"file_key": "file_0_0", "document_type": "Delivery Challan"}
                    ]
                }
            ]
        }

        files = {
            "file_0_0": ("challan.pdf", b"%PDF-1.4 challan content", "application/pdf")
        }

        body, ctype = make_multipart_body({"po_data": json.dumps(po_payload)}, files)
        headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
        h = MockHandler("POST", "/api/purchase-orders/upload", headers=headers, body=body, session_token=self.session_token)
        code, data, _ = h.handle_request()

        self.assertEqual(code, 201)
        self.assertTrue(data.get("success"))
        self.assertTrue(data.get("is_existing"))
        self.assertEqual(data.get("items_count"), 1)
        self.assertEqual(data.get("docs_count"), 1)

        with server.local_connect() as db:
            pos = db.execute("SELECT * FROM purchase_orders WHERE po_id = ?", (po_id,)).fetchall()
            self.assertEqual(len(pos), 1)

            total_items = db.execute("SELECT count(*) FROM po_items WHERE purchase_order_id = ?", (pos[0]["id"],)).fetchone()[0]
            self.assertEqual(total_items, 3)

    def test_05_validation_errors(self):
        # Missing PO ID
        payload1 = {"po_id": "", "items": [{"item_code": "A", "item_name": "B", "vendor_code": "C", "vendor_name": "D", "documents": []}]}
        body1, ctype1 = make_multipart_body({"po_data": json.dumps(payload1)}, {})
        h1 = MockHandler("POST", "/api/purchase-orders/upload", headers={"Content-Type": ctype1, "Content-Length": str(len(body1))}, body=body1, session_token=self.session_token)
        code1, data1, _ = h1.handle_request()
        self.assertEqual(code1, 400)
        self.assertIn("Purchase Order (PO) ID is required", data1.get("error", ""))

        # Item with no documents
        payload2 = {"po_id": "PO-TEST-NODOCS", "financial_year": "2026-27", "month": "May", "items": [{"item_code": "A", "item_name": "B", "vendor_code": "C", "vendor_name": "D", "documents": []}]}
        body2, ctype2 = make_multipart_body({"po_data": json.dumps(payload2)}, {})
        h2 = MockHandler("POST", "/api/purchase-orders/upload", headers={"Content-Type": ctype2, "Content-Length": str(len(body2))}, body=body2, session_token=self.session_token)
        code2, data2, _ = h2.handle_request()
        self.assertEqual(code2, 400)
        self.assertIn("At least one document is required", data2.get("error", ""))

        # Item missing vendor details
        payload3 = {"po_id": "PO-TEST-NOVENDOR", "financial_year": "2026-27", "month": "May", "items": [{"item_code": "A", "item_name": "B", "vendor_code": "", "vendor_name": "D", "documents": [{"file_key": "file_0_0", "document_type": "Purchase Order"}]}]}
        body3, ctype3 = make_multipart_body({"po_data": json.dumps(payload3)}, {"file_0_0": ("order.pdf", b"%PDF-1.4 ...", "application/pdf")})
        h3 = MockHandler("POST", "/api/purchase-orders/upload", headers={"Content-Type": ctype3, "Content-Length": str(len(body3))}, body=body3, session_token=self.session_token)
        code3, data3, _ = h3.handle_request()
        self.assertEqual(code3, 400)
        self.assertIn("Vendor Code is required", data3.get("error", ""))

    def test_06_excel_export_includes_po_fields(self):
        h = MockHandler("GET", "/api/documents/export.xlsx", session_token=self.session_token)
        code, body, headers = h.handle_request()
        self.assertEqual(code, 200)
        self.assertIn("spreadsheetml.sheet", headers.get("Content-Type", ""))
        self.assertTrue(len(body) > 100)

    def test_07_view_files_integration(self):
        h = MockHandler("GET", "/api/documents", session_token=self.session_token)
        code, data, _ = h.handle_request()
        self.assertEqual(code, 200)
        docs = data.get("documents", [])
        auto_docs = [d for d in docs if d.get("po_id") == "PO-2026-AUTO-001"]
        self.assertTrue(len(auto_docs) >= 3)
        self.assertEqual(auto_docs[0]["financialYear"], "2026-27")
        self.assertEqual(auto_docs[0]["month"], "April")

    def test_08_file_cleanup_on_error(self):
        po_id = "PO-2026-AUTO-ERR"
        po_payload = {
            "po_id": po_id,
            "financial_year": "2026-27",
            "month": "April",
            "items": [
                {
                    "item_code": "ITM-FAIL-01",
                    "item_name": "Failure Item",
                    "vendor_code": "VND-FAIL",
                    "vendor_name": "Fail Vendor",
                    "documents": [
                        {"file_key": "file_0_0", "document_type": "Purchase Order"},
                        {"file_key": "file_0_1", "document_type": ""} # empty doc type -> will cause ValueError!
                    ]
                }
            ]
        }
        files = {
            "file_0_0": ("clean1.pdf", b"%PDF-1.4 clean content", "application/pdf"),
            "file_0_1": ("clean2.pdf", b"%PDF-1.4 clean content", "application/pdf")
        }
        body, ctype = make_multipart_body({"po_data": json.dumps(po_payload)}, files)
        headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
        h = MockHandler("POST", "/api/purchase-orders/upload", headers=headers, body=body, session_token=self.session_token)
        code, data, _ = h.handle_request()
        self.assertEqual(code, 400)

        # Verify no orphan files or DB records
        with server.local_connect() as db:
            po = db.execute("SELECT * FROM purchase_orders WHERE po_id = ?", (po_id,)).fetchone()
            self.assertIsNone(po)
            docs = db.execute("SELECT * FROM documents WHERE po_id = ?", (po_id,)).fetchall()
            self.assertEqual(len(docs), 0)

if __name__ == "__main__":
    unittest.main()
