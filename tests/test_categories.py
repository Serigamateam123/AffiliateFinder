import categories


def test_known_id_maps_to_name():
    assert categories.name("700645") == "Health"
    assert categories.name(601450) == "Beauty & Personal Care"


def test_unknown_id_passes_through_raw():
    assert categories.name("999999") == "999999"


def test_names_list_and_empty():
    assert categories.names(["700645", "999999"]) == ["Health", "999999"]
    assert categories.names(None) == []
