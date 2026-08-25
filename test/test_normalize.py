"""Tests for the normalizer. These are the contract -- written before normalize.py.

Run with:  python3 -m unittest discover -s test -v
       or:  python3 -m pytest test -q
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import normalize as N  # noqa: E402


STORE = {
    "name": "Aura Fragrance",
    "domain": "aurafragrance.com",
    "sells_dupes": False,
}
DUPE_STORE = dict(STORE, name="Aromatix", domain="shoparomatix.com", sells_dupes=True)


def product(title, vendor="Parfums de Marly", product_type="", handle="p"):
    return {"id": 1, "title": title, "vendor": vendor, "product_type": product_type,
            "handle": handle, "variants": []}


def variant(title="Default Title", price="100.00", available=True, vid=11):
    return {"id": vid, "title": title, "price": price, "available": available}


class TestSize(unittest.TestCase):
    def test_oz_snaps_to_100(self):
        # 3.4 * 29.5735 = 100.55 -> within 3ml of 100 -> snap
        self.assertEqual(N.parse_size_ml("3.4 OZ Tester Box (same liquid, plainer box)"), 100)

    def test_oz_snaps_to_50(self):
        self.assertEqual(N.parse_size_ml("1.7 OZ Regular Box"), 50)

    def test_plain_ml_is_trusted(self):
        self.assertEqual(N.parse_size_ml("125ml"), 125)
        self.assertEqual(N.parse_size_ml("100 ML Eau de Parfum"), 100)

    def test_ml_wins_over_oz_when_both_present(self):
        self.assertEqual(N.parse_size_ml("3.4 oz / 100 ml"), 100)

    def test_variant_title_beats_product_title(self):
        self.assertEqual(N.parse_size_ml("50ml", "Layton 125ml"), 50)

    def test_falls_through_empty_variant_title(self):
        self.assertEqual(N.parse_size_ml("Default Title", "Layton 125ml"), 125)

    def test_odd_oz_does_not_snap(self):
        # 8.4 oz = 248.4ml, no standard within 3ml -> keep converted value
        self.assertAlmostEqual(N.parse_size_ml("8.4 oz"), 248.4, places=1)

    def test_no_size(self):
        self.assertIsNone(N.parse_size_ml("Layton Eau de Parfum"))


class TestConcentration(unittest.TestCase):
    def test_product_type_extrait(self):
        self.assertEqual(N.parse_concentration("Extrait de Parfum", "Layton"), "Extrait")

    def test_product_type_wins_over_title(self):
        self.assertEqual(N.parse_concentration("Eau De Parfum", "Layton EDT"), "EDP")

    def test_falls_back_to_title(self):
        self.assertEqual(N.parse_concentration("Fragrance", "Layton Eau de Toilette"), "EDT")
        self.assertEqual(N.parse_concentration("", "Layton EDP 125ml"), "EDP")
        self.assertEqual(N.parse_concentration(None, "Bleu de Chanel EDC"), "EDC")

    def test_elixir_and_parfum(self):
        self.assertEqual(N.parse_concentration("", "Sauvage Elixir"), "Elixir")
        self.assertEqual(N.parse_concentration("", "Coco Mademoiselle Parfum"), "Parfum")
        self.assertEqual(N.parse_concentration("", "Acqua di Gio Cologne"), "Cologne")

    def test_never_guesses(self):
        self.assertIsNone(N.parse_concentration("Fragrance", "Layton 125ml"))
        self.assertIsNone(N.parse_concentration(None, "Haltane Tester"))


class TestConditionAndPreorder(unittest.TestCase):
    def test_tester_beats_box_wording(self):
        self.assertEqual(N.parse_condition("3.4 OZ Tester Box (same liquid, plainer box)"), "tester")

    def test_regular_box_is_sealed(self):
        self.assertEqual(N.parse_condition("1.7 OZ Regular Box"), "sealed")

    def test_unboxed(self):
        self.assertEqual(N.parse_condition("Layton 125ml (No Box)"), "unboxed")
        self.assertEqual(N.parse_condition("Layton Unboxed"), "unboxed")

    def test_unknown(self):
        self.assertEqual(N.parse_condition("Layton 125ml"), "unknown")

    def test_preorder_spaced_hyphen(self):
        self.assertTrue(N.parse_preorder("Haltane Tester (PRE - ORDER)"))
        self.assertTrue(N.parse_preorder("Preorder: Layton"))
        self.assertTrue(N.parse_preorder("Layton pre order"))
        self.assertFalse(N.parse_preorder("Layton 125ml"))

    def test_haltane_row(self):
        cond = N.parse_condition("Haltane Tester (PRE - ORDER)")
        self.assertEqual(cond, "tester")
        self.assertTrue(N.parse_preorder("Haltane Tester (PRE - ORDER)"))


class TestExclusions(unittest.TestCase):
    def test_gift_set_excluded(self):
        self.assertIsNotNone(N.excluded_reason("Parfums de Marly Layton Gift Set"))
        self.assertIsNotNone(N.excluded_reason("Discovery Set - 8 x 2ml"))
        self.assertIsNotNone(N.excluded_reason("Set of 3 Miniatures"))
        self.assertIsNotNone(N.excluded_reason("Layton Bundle"))

    def test_non_fragrance_excluded(self):
        for t in ["Layton Deodorant", "Sauvage Deo Stick", "Shower Gel 200ml",
                  "Body Lotion 200ml", "Body Cream", "Body Oil", "Body Wash",
                  "Scented Candle", "Reed Diffuser", "Layton 2ml Sample",
                  "Layton Decant 10ml", "Travel Spray 10ml", "Refill 3x20ml",
                  "Layton Mini 7ml", "Rollerball 10ml", "Hair Mist 75ml",
                  "Perfume Oil 12ml", "VIP Membership", "Monthly Subscription",
                  "Shipping Insurance", "Gift Wrap Add-On"]:
            self.assertIsNotNone(N.excluded_reason(t), t)

    def test_real_bottles_not_excluded(self):
        for t in ["Parfums de Marly Layton 125ml EDP", "Sunset Boulevard 100ml",
                  "Dominant Extreme 100ml", "Minimalist 100ml EDP"]:
            self.assertIsNone(N.excluded_reason(t), t)


class TestBrandAndLine(unittest.TestCase):
    def test_strips_general_and_store_name(self):
        self.assertEqual(N.clean_brand("General", "Aura Fragrance"), None)
        self.assertEqual(N.clean_brand("Aura Fragrance", "Aura Fragrance"), None)
        self.assertEqual(N.clean_brand("General Parfums de Marly", "Aura Fragrance"),
                         "Parfums de Marly")
        self.assertEqual(N.clean_brand("Parfums de Marly", "Aura Fragrance"),
                         "Parfums de Marly")

    def test_line_strips_everything_but_the_name(self):
        self.assertEqual(
            N.parse_line("Parfums de Marly Layton Eau de Parfum 125ml for Men",
                         "Parfums de Marly", "EDP"),
            "Layton")
        self.assertEqual(
            N.parse_line("Layton EDP 3.4 OZ Tester Box (PRE - ORDER)",
                         "Parfums de Marly", "EDP"),
            "Layton")
        self.assertEqual(
            N.parse_line("New Bleu de Chanel by Chanel Spray for men 100ml",
                         "Chanel", None),
            "Bleu de Chanel")


class TestSlugAndKey(unittest.TestCase):
    def test_slug_folds_accents_and_case(self):
        self.assertEqual(N.slugify("L'Immensité"), "l-immensite")
        self.assertEqual(N.slugify("Parfums de Marly"), "parfums-de-marly")

    def test_key_collapses_sizes(self):
        k1 = N.canonical_key("Parfums de Marly", "Layton", "EDP", False)
        k2 = N.canonical_key("parfums de marly", "layton", "EDP", False)
        self.assertEqual(k1, k2)
        self.assertEqual(k1, "parfums-de-marly|layton|EDP")

    def test_dupe_store_never_merges(self):
        self.assertEqual(N.canonical_key("Lattafa", "Khamrah", "EDP", True),
                         "dupe:lattafa|khamrah|EDP")


class TestNormalizeVariant(unittest.TestCase):
    def test_full_row(self):
        p = product("Parfums de Marly Layton Eau de Parfum for Men",
                    product_type="Eau De Parfum", handle="pdm-layton")
        v = variant("3.4 OZ Tester Box (same liquid, plainer box)", "159.99")
        row = N.normalize_variant(p, v, STORE)
        self.assertIsNotNone(row)
        self.assertEqual(row["brand"], "Parfums de Marly")
        self.assertEqual(row["line"], "Layton")
        self.assertEqual(row["concentration"], "EDP")
        self.assertEqual(row["size_ml"], 100)
        self.assertEqual(row["condition"], "tester")
        self.assertFalse(row["preorder"])
        self.assertEqual(row["price"], 159.99)
        self.assertEqual(row["key"], "parfums-de-marly|layton|EDP")
        self.assertEqual(row["url"],
                         "https://aurafragrance.com/products/pdm-layton?variant=11")

    def test_regular_box_is_sealed_50ml(self):
        p = product("Parfums de Marly Layton", product_type="Extrait de Parfum")
        row = N.normalize_variant(p, variant("1.7 OZ Regular Box", "119.00"), STORE)
        self.assertEqual(row["size_ml"], 50)
        self.assertEqual(row["condition"], "sealed")
        self.assertEqual(row["concentration"], "Extrait")

    def test_preorder_row_is_kept_but_flagged(self):
        p = product("Parfums de Marly Haltane", product_type="Eau De Parfum")
        row = N.normalize_variant(p, variant("Haltane Tester (PRE - ORDER) 125ml", "175"), STORE)
        self.assertIsNotNone(row)
        self.assertTrue(row["preorder"])
        self.assertEqual(row["condition"], "tester")

    def test_gift_set_dropped(self):
        p = product("Parfums de Marly Layton Gift Set", product_type="Eau De Parfum")
        row, reason = N.normalize_variant_ex(p, variant("125ml + 10ml", "199"), STORE)
        self.assertIsNone(row)
        self.assertTrue(reason.startswith("excluded:"))

    def test_size_floor_drops_30ml(self):
        p = product("Parfums de Marly Layton", product_type="Eau De Parfum")
        row, reason = N.normalize_variant_ex(p, variant("30ml", "79"), STORE)
        self.assertIsNone(row)
        self.assertEqual(reason, "below_size_floor:30.0")

    def test_out_of_stock_dropped(self):
        p = product("Parfums de Marly Layton", product_type="Eau De Parfum")
        row, reason = N.normalize_variant_ex(p, variant("125ml", "119", available=False), STORE)
        self.assertIsNone(row)
        self.assertEqual(reason, "unavailable")

    def test_unknown_size_dropped(self):
        p = product("Parfums de Marly Layton", product_type="Eau De Parfum")
        row, reason = N.normalize_variant_ex(p, variant("Default Title", "119"), STORE)
        self.assertIsNone(row)
        self.assertEqual(reason, "no_size")

    def test_tester_is_never_filtered_out(self):
        p = product("Lattafa Khamrah", product_type="Eau De Parfum")
        row = N.normalize_variant(p, variant("100ml Tester", "29.99"), DUPE_STORE)
        self.assertIsNotNone(row)
        self.assertEqual(row["condition"], "tester")
        self.assertTrue(row["key"].startswith("dupe:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestBrandPrefixVariants(unittest.TestCase):
    """Stores rarely print the vendor string verbatim in the title."""

    def test_leading_brand_token(self):
        self.assertEqual(
            N.parse_line("Initio Oud For Greatness 90ml Extrait",
                         "Initio Parfums Prives", "Extrait"),
            "Oud For Greatness")

    def test_trailing_brand_token(self):
        self.assertEqual(
            N.parse_line("Dior Sauvage Elixir 100ml", "Christian Dior", "Elixir"),
            "Sauvage")

    def test_longest_run_wins(self):
        self.assertEqual(
            N.parse_line("Van Cleef Collection Extraordinaire Rose Rouge 75ml",
                         "Van Cleef & Arpels", None),
            "Collection Extraordinaire Rose Rouge")

    def test_never_strips_the_whole_name(self):
        self.assertEqual(N.parse_line("Chanel", "Chanel", None), "Chanel")

    def test_brand_inside_name_survives(self):
        self.assertEqual(
            N.parse_line("Bleu de Chanel EDT 100ml", "Chanel", "EDT"),
            "Bleu de Chanel")
