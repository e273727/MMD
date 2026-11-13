import email.message
import io
import json
import os
import sqlite3
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
        # Find header end
        idx = raw_val.find(b"\r\n\r\n")
        body_bytes = raw_val[idx+4:] if idx != -1 else raw_val
        try:
            return self.response_code, json.loads(body_bytes.decode("utf-8")), self.response_headers
        except Exception:
            return self.response_code, body_bytes, self.response_headers


class TestViewAndSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.initialize()
        cls.session_token = server.create_session("admin_user_id")

    def test_01_get_documents_api(self):
        """Verify /api/documents returns all documents with PO and classification fields."""
        handler = MockHandler("GET", "/api/documents", session_token=self.session_token)
        code, data, _ = handler.handle_request()
        self.assertEqual(code, 200)
        self.assertIn("documents", data)
        docs = data["documents"]
        self.assertGreaterEqual(len(docs), 16)
        for d in docs:
            self.assertIn("id", d)
            self.assertIn("name", d)
            self.assertIn("po_id", d)
            self.assertTrue(len(d["po_id"]) > 0, f"Document {d['id']} missing po_id")
            self.assertIn("financialYear", d)
            self.assertIn("item_code", d)
            self.assertIn("itemText", d)
            self.assertIn("vendor_code", d)
            self.assertIn("vendorText", d)
            self.assertIn("documentType", d)
            self.assertIn("serialKey", d)

    def test_02_file_serving_api(self):
        """Verify /api/file/<id> and /api/documents/<id>/file serve actual files."""
        with server.connect() as c:
            row = c.execute("SELECT id, name, media_type FROM documents LIMIT 1").fetchone()
        doc_id = row["id"]
        
        # Test /api/file/<id>
        handler1 = MockHandler("GET", f"/api/file/{doc_id}", session_token=self.session_token)
        code1, data1, headers1 = handler1.handle_request()
        self.assertEqual(code1, 200)
        self.assertEqual(headers1.get("Content-Type"), row["media_type"] or "application/pdf")
        self.assertGreater(len(data1), 0)

        # Test /api/documents/<id>/file
        handler2 = MockHandler("GET", f"/api/documents/{doc_id}/file", session_token=self.session_token)
        code2, data2, headers2 = handler2.handle_request()
        self.assertEqual(code2, 200)
        self.assertEqual(len(data2), len(data1))

    def test_03_file_serving_missing_file_graceful_error(self):
        """Verify missing file or non-existent document returns friendly 404 HTML."""
        handler = MockHandler("GET", "/api/file/NON_EXISTENT_ID_9999", session_token=self.session_token)
        code, body, _ = handler.handle_request()
        self.assertEqual(code, 404)
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        self.assertIn("Document Record Not Found", body)

    def test_04_po_id_search_simulation(self):
        """Verify documents can be filtered by PO ID alone, Financial Year alone, and combined."""
        handler = MockHandler("GET", "/api/documents", session_token=self.session_token)
        code, data, _ = handler.handle_request()
        docs = data["documents"]

        # Search PO ID alone: 'MMD/2026-27/0010'
        po_results = [d for d in docs if 'MMD/2026-27/0010'.lower() in (d.get('po_id') or '').lower()]
        self.assertEqual(len(po_results), 3)
        for d in po_results:
            self.assertEqual(d['po_id'], 'MMD/2026-27/0010')
            self.assertTrue(d['item_code'] or d['itemText'])

        # Search Financial Year alone: '2026-27'
        fy_results = [d for d in docs if '2026-27'.lower() in (d.get('financialYear') or '').lower()]
        self.assertGreaterEqual(len(fy_results), 3)

        # Combined PO ID and Financial Year
        combined = [
            d for d in docs
            if 'MMD/2026-27/0010'.lower() in (d.get('po_id') or '').lower()
            and '2026-27'.lower() in (d.get('financialYear') or '').lower()
        ]
        self.assertEqual(len(combined), 3)

        # Combined mismatch: PO 'MMD/2026-27/0010' with FY '2025-26' -> should be empty
        mismatch = [
            d for d in docs
            if 'MMD/2026-27/0010'.lower() in (d.get('po_id') or '').lower()
            and '2025-26'.lower() in (d.get('financialYear') or '').lower()
        ]
        self.assertEqual(len(mismatch), 0)

    def test_05_all_16_files_accessible(self):
        """Verify all 16 documents in database are physically accessible through /api/file/<id>."""
        handler = MockHandler("GET", "/api/documents", session_token=self.session_token)
        code, data, _ = handler.handle_request()
        docs = data["documents"]

        for d in docs:
            fhandler = MockHandler("GET", f"/api/file/{d['id']}", session_token=self.session_token)
            fcode, fdata, fheaders = fhandler.handle_request()
            self.assertEqual(fcode, 200, f"File {d['name']} ({d['id']}) failed to load: {fdata}")
            self.assertGreater(len(fdata), 0, f"File {d['name']} is empty")

if __name__ == '__main__':
    unittest.main()
