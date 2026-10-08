import unittest

import check_stock


class ScraperHelperTests(unittest.TestCase):
    def test_unknown_option_field_is_not_false_soldout(self):
        parsed, diagnostic = check_stock.parse_option_stock({
            "item-code": {"option_value": "M", "unexpected": "value"}
        })
        self.assertEqual(parsed, {"M": -1})
        self.assertIn(check_stock.UNCERTAIN_MARKER, diagnostic)
        self.assertFalse(check_stock.is_fully_sold_out(parsed))

    def test_bad_json_is_uncertain_failure(self):
        parsed, diagnostic = check_stock.parse_option_stock("not json")
        self.assertIsNone(parsed)
        self.assertIn(check_stock.UNCERTAIN_MARKER, diagnostic)

    def test_image_url_is_absolute_https(self):
        self.assertEqual(
            check_stock.normalize_image_url(
                "//cdn.example.com/images/a.jpg?size=large",
                "http://shop.example/product/a/1/",
            ),
            "https://cdn.example.com/images/a.jpg?size=large",
        )
        self.assertEqual(
            check_stock.normalize_image_url(
                "/images/a.jpg", "https://shop.example/product/a/1/"
            ),
            "https://shop.example/images/a.jpg",
        )
        self.assertIsNone(
            check_stock.normalize_image_url(
                "data:image/png;base64,abc", "https://shop.example/product/a/1/"
            )
        )

    def test_cap_and_unknown_display(self):
        self.assertEqual(check_stock.stock_status(-1)[1], "확인불가")
        self.assertIn("실제 재고 아님", check_stock.stock_status(9999)[1])


if __name__ == "__main__":
    unittest.main()
