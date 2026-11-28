import copy
import json
import unittest
from pathlib import Path

from pkgwatch import carriers
from pkgwatch.carriers import asl, generic, intelcom, precision
from pkgwatch.classify import classify
from pkgwatch.models import Status, TrackingError

FX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FX / name).read_text())


class ClassifyTest(unittest.TestCase):
    CASES = {
        "Out for Delivery": Status.OUT_FOR_DELIVERY,
        "Outbound onto Delivery Truck": Status.OUT_FOR_DELIVERY,
        "Delivered": Status.DELIVERED,
        "Delivered - Front door": Status.DELIVERED,
        "Delivery attempted": Status.ATTEMPTED,
        "Attempted delivery - no access": Status.ATTEMPTED,
        "Not delivered - customer unavailable": Status.ATTEMPTED,
        "Undeliverable": Status.ATTEMPTED,
        "Returned to sender": Status.RETURNED,
        "Delivery exception": Status.EXCEPTION,
        "Shipment delayed": Status.EXCEPTION,
        "Available for pickup": Status.AVAILABLE_FOR_PICKUP,
        "Manifest Scan": Status.INFO_RECEIVED,
        "Data received": Status.INFO_RECEIVED,
        "Received at Terminal": Status.IN_TRANSIT,
        "Hub station inbound scan": Status.IN_TRANSIT,
        "Livré": Status.DELIVERED,
        "En livraison": Status.OUT_FOR_DELIVERY,
        "something odd": Status.UNKNOWN,
    }

    def test_cases(self):
        for text, want in self.CASES.items():
            with self.subTest(text=text):
                self.assertEqual(classify(text), want)

    def test_out_for_delivery_never_delivered(self):
        self.assertNotEqual(classify("Out for delivery — estimated delivery today"), Status.DELIVERED)


class IntelcomTest(unittest.TestCase):
    def test_delivered_fixture(self):
        r = intelcom.parse(load("intelcom_pickup_delivered.json"))
        self.assertEqual(r.status, Status.DELIVERED)
        self.assertEqual(r.label, "Package Picked Up")
        self.assertAlmostEqual(r.event_time, 1795655790.697, places=2)
        self.assertEqual([e.status for e in r.events][:2], ["delivered", "available_for_pickup"])
        self.assertFalse(r.date_only)

    def test_ready_for_pickup(self):
        d = load("intelcom_pickup_delivered.json")
        res = d["data"]["result"]
        res["status_list"] = res["status_list"][1:]
        res["last_status"] = res["status_list"][0]
        r = intelcom.parse(d)
        self.assertEqual(r.status, Status.AVAILABLE_FOR_PICKUP)

    def test_in_transit_step_fallback(self):
        d = load("intelcom_pickup_delivered.json")
        last = copy.deepcopy(d["data"]["result"]["status_list"][2])
        last.update(label="Mystery status", status=999, step=3)
        last["labels"]["shortLabel"]["en"] = ""
        d["data"]["result"]["last_status"] = last
        self.assertEqual(intelcom.parse(d).status, Status.OUT_FOR_DELIVERY)

    def test_delivered_text_without_flag_does_not_alarm(self):
        d = load("intelcom_pickup_delivered.json")
        last = d["data"]["result"]["last_status"]
        last.update(isDelivered=False, status=601, label="Delivered?")
        self.assertNotEqual(intelcom.parse(d).status, Status.DELIVERED)

    def test_not_found(self):
        r = intelcom.parse({"success": False, "data": {"status": 404, "code": "not_found"}})
        self.assertEqual(r.status, Status.NOT_FOUND)

    def test_error_is_error_not_status(self):
        with self.assertRaises(TrackingError):
            intelcom.parse({"success": False, "data": {"code": "internal"}})
        with self.assertRaises(TrackingError):
            intelcom.parse({"success": True, "data": {"result": None}})


class ASLTest(unittest.TestCase):
    def test_out_for_delivery(self):
        r = asl.parse(load("asl_out_for_delivery.json"))
        self.assertEqual(r.status, Status.OUT_FOR_DELIVERY)
        self.assertEqual(r.sub_carrier, "Precision Parcel")
        self.assertEqual(r.eta, "November 26, 2026")
        self.assertTrue(r.date_only)
        self.assertEqual([e.label for e in r.events], ["Out for Delivery", "Received at Terminal", "Manifest Scan"])
        self.assertEqual(r.events[0].location, "Montreal, QC")

    def _with_label(self, label):
        d = load("asl_out_for_delivery.json")
        d["data"]["html"] = d["data"]["html"].replace(">Out for Delivery<", f">{label}<", 1)
        return asl.parse(d)

    def test_delivered(self):
        self.assertEqual(self._with_label("Delivered").status, Status.DELIVERED)

    def test_attempted(self):
        self.assertEqual(self._with_label("Delivery Attempted").status, Status.ATTEMPTED)

    def test_layout_change_is_error(self):
        with self.assertRaises(TrackingError):
            asl.parse({"success": True, "data": {"html": "<div>new layout</div>"}})

    def test_rate_limited(self):
        with self.assertRaises(TrackingError) as cm:
            asl.parse({"success": False, "data": {"code": "rate_limited"}})
        self.assertTrue(cm.exception.retry_after)


class PrecisionTest(unittest.TestCase):
    def test_out_for_delivery_exact_time(self):
        r = precision.parse(load("precision_out_for_delivery.json"))
        self.assertEqual(r.status, Status.OUT_FOR_DELIVERY)
        self.assertEqual(r.event_time, 1795698665.0)  # 2026-11-26 13:11:05 UTC = 08:11:05 EST
        self.assertFalse(r.date_only)

    def test_signature_means_delivered(self):
        d = load("precision_out_for_delivery.json")
        ev = copy.deepcopy(d["scanevents"][-1])
        ev.update(scandatetime="2026-11-26 18:00:00", status="DLV", publicstatus="Package left", signedby="FRONT DOOR")
        d["scanevents"].append(ev)
        r = precision.parse(d)
        self.assertEqual(r.status, Status.DELIVERED)
        self.assertEqual(r.label, "Package left")


class GenericTest(unittest.TestCase):
    FILLER = "<p>" + "Lorem ipsum tracking page filler text. " * 10 + "</p>"

    def test_phrases(self):
        cases = {
            "Your package was delivered on Nov 26": Status.DELIVERED,
            "Status: Out for delivery": Status.OUT_FOR_DELIVERY,
            "Delivery attempted - we'll try again": Status.ATTEMPTED,
            "FAQ: what if my package is not delivered?": Status.UNKNOWN,
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(generic.analyse(f"<html>{self.FILLER}<div>{text}</div></html>").status, want)

    def test_custom_phrase(self):
        page = f"<html>{self.FILLER}<b>Delivered</b></html>"
        self.assertEqual(generic.analyse(page, "Delivered").status, Status.DELIVERED)
        self.assertEqual(generic.analyse(page, "Signed").status, Status.UNKNOWN)

    def test_captcha_and_js_apps_are_errors(self):
        with self.assertRaises(TrackingError):
            generic.analyse(f'<html>{self.FILLER}<div class="g-recaptcha"></div></html>')
        with self.assertRaises(TrackingError):
            generic.analyse('<html><div id="root"></div><script>app()</script></html>')


class ResolveTest(unittest.TestCase):
    def test_detection(self):
        self.assertEqual(carriers.resolve("ASL0012345678")[0], "asl")
        self.assertEqual(carriers.resolve(" delcf000123456 ")[:2], ("intelcom", "DELCF000123456"))
        self.assertEqual(carriers.resolve("SIM-OFD,DEL")[:2], ("sim", "SIM-OFD,DEL"))

    def test_urls(self):
        self.assertEqual(carriers.resolve("https://intelcom.ca/en/track-your-package/?tracking-id=DELCF000123456")[:2],
                         ("intelcom", "DELCF000123456"))
        self.assertEqual(carriers.resolve("https://asldistribution.com/tracking/?tracking=ASL0012345678")[:2],
                         ("asl", "ASL0012345678"))
        self.assertEqual(carriers.resolve("https://some-carrier.example/track?id=1")[0], "generic")

    def test_manual(self):
        self.assertEqual(carriers.resolve("ASL0012345678", "precision")[0], "precision")
        with self.assertRaises(ValueError):
            carriers.resolve("ABC", "nope")


class ASLHttp404Test(unittest.TestCase):
    def test_http_404_not_found_means_not_in_system_yet(self):
        from pkgwatch.http import Response
        a = asl.ASL()
        a._get_nonce = lambda: ""
        a.session.post = lambda url, **kw: (
            Response(404, {}, b'{"success":false,"data":{"code":"not_found"}}', url)
            if 404 in kw.get("ok_statuses", ()) else (_ for _ in ()).throw(TrackingError("HTTP 404")))
        from pkgwatch.models import Package
        r = a._track_asl(Package(tracking_number="ASL0000000001", carrier="asl"))
        self.assertEqual(r.status, Status.NOT_FOUND)

    def test_http_404_without_json_is_still_an_error(self):
        from pkgwatch.http import Response
        from pkgwatch.models import Package
        a = asl.ASL()
        a._get_nonce = lambda: ""
        a.session.post = lambda url, **kw: Response(404, {}, b"<html>gone</html>", url)
        with self.assertRaises(TrackingError):
            a._track_asl(Package(tracking_number="ASL0000000001", carrier="asl"))


class EtaTest(unittest.TestCase):
    def test_intelcom_raw_eta_normalized(self):
        self.assertEqual(intelcom.eta_of({"eta": "2026-11-28T10:42:15.000000-05:00"}), "2026-11-28T10:42:15-05:00")

    def test_intelcom_public_window_preferred(self):
        r = {"eta": "2026-11-28T10:42:15.000000-05:00",
             "public_eta": {"from": "2026-11-28T15:00:00Z", "to": "2026-11-28T17:00:00Z", "min": 1, "max": 2}}
        self.assertEqual(intelcom.eta_of(r), "2026-11-28T15:00:00+00:00/2026-11-28T17:00:00+00:00")

    def test_intelcom_garbage_eta_is_empty_not_object(self):
        self.assertEqual(intelcom.eta_of({"eta": None, "public_eta": {"weird": 1}}), "")
        self.assertIsInstance(intelcom.parse(load("intelcom_pickup_delivered.json")).eta, str)

    def test_precision_edd_is_end_of_window(self):
        self.assertEqual(precision.parse(load("precision_out_for_delivery.json")).eta, "by:2026-11-26T17:00:00")


if __name__ == "__main__":
    unittest.main()
