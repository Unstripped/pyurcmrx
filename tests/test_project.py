import re

import pytest

from pyurcmrx import ProjectParseError, build_catalog, parse_mcs, parse_mcs_info

# Spec §30 §8.1 minimal project test vectors.
MCS = (
    '{"con_devs":[{"id":2,"name":"Living Room TV","db":{"type":0,"builtins":[],"zones":[{"idx":0,"funcs":'
    '[{"id":530,"name":"Power"},{"id":275,"name":"Vol+"},{"id":276,"name":"Vol-"},{"id":277,"name":"Mute"}]}]}}],'
    '"editor_ver":"CCP_BIN_VER_STR3.02.0628.4     ","editor_type":"URC Accelerator 2"}'
)
MCS_INFO = (
    '{"bstations":[{"obj_id":1,"name":"Master MRX-15","model":"MRX-15"}],"clients":[{"obj_id":5,"name":"Mobile Client",'
    '"model":"IPhoneV2"}],"devices":[{"obj_id":2,"name":"Living Room TV","model":"DBMODEL","db":{"type":"IR",'
    '"category":"TV","brand":"Generic","model":"GENERIC_TV"}}],"editor_ver":"CCP_BIN_VER_STR3.02.0628.4     ",'
    '"editor_type":"URC Accelerator 3","editor_name":"Accelerator 3 -","download_date":"2026-11-02 12:00:00",'
    '"file_indicator":1730548800,"special_jump":[{"Please Wait":10},{"Off Site Option":3},{"Sleep Timer":3},'
    '{"HDA Room EQ":1}]}'
)


def test_minimal_project_catalog():
    catalog = build_catalog(parse_mcs(MCS), parse_mcs_info(MCS_INFO.encode()))
    assert len(catalog) == 1
    entry = catalog[0]
    assert (entry.device.id, entry.device.name, entry.device.multi_zone) == (2, "Living Room TV", False)
    assert [(f.id, f.name) for f in entry.device.zones[0].funcs] == [(530, "Power"), (275, "Vol+"), (276, "Vol-"), (277, "Mute")]
    assert (entry.info.model, entry.info.transport, entry.info.category, entry.info.brand) == ("DBMODEL", "IR", "TV", "Generic")


def test_multi_zone_device_and_missing_info_entry():
    mcs = (
        '{"con_devs":[{"id":7,"name":"HDA","db":{"type":1,"builtins":[],"zones":['
        '{"idx":0,"funcs":[{"id":275,"name":"Vol+"}]},{"idx":1,"funcs":[{"id":277,"name":"Mute"}]}]}}],'
        '"editor_ver":"v","editor_type":"t"}'
    )
    info = MCS_INFO.replace('"obj_id":2', '"obj_id":3')
    (entry,) = build_catalog(parse_mcs(mcs), parse_mcs_info(info))
    assert entry.device.multi_zone
    assert [z.idx for z in entry.device.zones] == [0, 1]
    assert entry.info is None


@pytest.mark.parametrize(
    "doc, message",
    [
        ("not json", "invalid JSON"),
        ("[]", "root is not an object"),
        ('{"editor_ver":"v","editor_type":"t"}', "missing required field 'con_devs'"),
        (MCS.replace('"id":2,', '"id":true,', 1), "con_devs[0].id: expected int"),
    ],
)
def test_invalid_mcs_reports_location(doc, message):
    with pytest.raises(ProjectParseError, match=re.escape(message)):
        parse_mcs(doc)
