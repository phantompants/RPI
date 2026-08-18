from src import wifi_manager


def test_parse_terse_splits_fields():
    output = "eth0:ethernet:connected:Wired connection 1\nwlan0:wifi:disconnected:--\n"
    rows = wifi_manager.parse_terse(output, 4)
    assert rows == [
        ["eth0", "ethernet", "connected", "Wired connection 1"],
        ["wlan0", "wifi", "disconnected", "--"],
    ]


def test_parse_terse_handles_escaped_colons_in_field_values():
    # An SSID containing a literal colon is escaped by nmcli as "\:".
    output = r"*:Cafe\:Guest:64:WPA2" + "\n"
    rows = wifi_manager.parse_terse(output, 4)
    assert rows == [["*", "Cafe:Guest", "64", "WPA2"]]


def test_parse_terse_skips_blank_lines():
    output = "a:b\n\nc:d\n"
    rows = wifi_manager.parse_terse(output, 2)
    assert rows == [["a", "b"], ["c", "d"]]


def test_parse_terse_skips_short_rows():
    output = "only-one-field\nfull:row:here\n"
    rows = wifi_manager.parse_terse(output, 3)
    assert rows == [["full", "row", "here"]]
