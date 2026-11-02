import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stock_checker as sc  # noqa: E402

BUY_PAGE = """
<script>window.PRODUCT_SELECTION_BOOTSTRAP = {"productSelectionData":{"products":[
 {"partNumber":"MAAA1LL/A","dimensionColor":"night-sky","dimensionCapacity":"256gb","price":"x"},
 {"partNumber":"MAAA2LL/A","dimensionColor":"star-white","dimensionCapacity":"512gb"},
 {"partNumber":"MAAA3LL/A","dimensionColor":"night-sky","dimensionCapacity":"1tb"}
]}};</script>
"""

FULFILLMENT = {
    "body": {"content": {"pickupMessage": {"stores": [
        {"storeNumber": "R100", "storeName": "Mall One", "city": "Town",
         "partsAvailability": {
             "MAAA1LL/A": {"pickupDisplay": "available",
                           "messageTypes": {"regular": {
                               "storePickupProductTitle": "iPhone Duo 256GB Night Sky",
                               "storePickupQuote": "<b>Today</b> at Mall One"}}},
             "MAAA2LL/A": {"pickupDisplay": "unavailable"}}},
        {"storeNumber": "R200", "storeName": "Mall Two",
         "partsAvailability": {"MAAA1LL/A": {"pickupDisplay": "unavailable"}}},
    ]}}}
}


class Tests(unittest.TestCase):
    def test_parse_parts(self):
        parts = sc.parse_parts_from_html(BUY_PAGE)
        self.assertEqual(parts["MAAA1LL/A"], {"capacity": "256gb", "color": "night-sky"})
        self.assertEqual(len(parts), 3)

    def test_parse_parts_from_name(self):
        # Real apple.com format (Nov 2026): capacity/color only in "name".
        html = ('{"items":[{"sku":"MK1H4","partNumber":"MK1H4LL/A","name":"iPhone Duo 512GB Night Sky"},'
                '{"sku":"MK1E4","partNumber":"MK1E4LL/A","name":"iPhone Duo 256GB Star White"}]}')
        parts = sc.parse_parts_from_html(html)
        self.assertEqual(parts["MK1H4LL/A"], {"capacity": "512gb", "color": "Night Sky"})
        self.assertEqual(parts["MK1E4LL/A"], {"capacity": "256gb", "color": "Star White"})

    def test_parse_stores(self):
        stores = sc.parse_stores(FULFILLMENT)
        self.assertEqual([s["number"] for s in stores], ["R100", "R200"])
        p = stores[0]["parts"]["MAAA1LL/A"]
        self.assertTrue(p["available"])
        self.assertEqual(p["quote"], "Today at Mall One")
        self.assertFalse(stores[0]["parts"]["MAAA2LL/A"]["available"])

    def test_dedupe(self):
        hit = {"store_number": "R100", "part": "MAAA1LL/A"}
        state = {}
        self.assertEqual(len(sc.new_hits_to_alert([hit], state, 60)), 1)
        self.assertEqual(len(sc.new_hits_to_alert([hit], state, 60)), 0)  # already alerted
        sc.new_hits_to_alert([], state, 60)                               # sold out
        self.assertEqual(len(sc.new_hits_to_alert([hit], state, 60)), 1)  # back in stock


if __name__ == "__main__":
    unittest.main()
