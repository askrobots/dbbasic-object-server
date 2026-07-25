"""OFX/QFX statement parsing (plan/bank-import-reconciliation-spec.md,
"v1.5: action_import_ofx").

The tests are the simulated bank feed, in both dialects that hide under one
file extension: an OFX 1.x SGML export with the unclosed leaf tags real
banks actually produce, and an OFX 2.x XML export of the identical
statement, pinned to produce the identical canonical shape. What matters
here is not "does an XML-ish string parse" but: dates are taken as printed
(never timezone-shifted), amounts are exact integer cents via Decimal,
FITID becomes the strong external_id, LEDGERBAL absence is None (not 0),
and a hostile DOCTYPE/ENTITY is refused before either parser ever runs.
"""

import pytest

import object_banking
import object_ofx
from object_ofx import OfxError


# --- fixtures -----------------------------------------------------------------
#
# The SGML statement is deliberately written the way real OFX 1.x exports
# look: header block, blank line, then leaf tags with no closing tag
# (</DTPOSTED> etc. never appear). The XML statement carries the same two
# transactions and the same balance, fully closed, to pin that both
# dialects land in the same canonical shape.

SGML_STATEMENT = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1>
<SONRS>
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<DTSERVER>20260710120000[-5:EST]
<LANGUAGE>ENG
</SONRS>
</SIGNONMSGSRSV1>
<BANKMSGSRSV1>
<STMTTRNRS>
<TRNUID>1
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<STMTRS>
<CURDEF>USD
<BANKACCTFROM>
<BANKID>123456789
<ACCTID>987654321
<ACCTTYPE>CHECKING
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260701000000[-5:EST]
<DTEND>20260710000000[-5:EST]
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260702120000[-5:EST]
<TRNAMT>1500.00
<FITID>2026070200001
<NAME>ACME CORP PAYMENT
<MEMO>INV-1001
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260705
<TRNAMT>-25.00
<FITID>2026070500001
<NAME>MONTHLY SERVICE FEE
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>1475.00
<DTASOF>20260710120000[-5:EST]
</LEDGERBAL>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""

XML_STATEMENT = """<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="200" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <SIGNONMSGSRSV1>
    <SONRS>
      <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
      <DTSERVER>20260710120000</DTSERVER>
      <LANGUAGE>ENG</LANGUAGE>
    </SONRS>
  </SIGNONMSGSRSV1>
  <BANKMSGSRSV1>
    <STMTTRNRS>
      <TRNUID>1</TRNUID>
      <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
      <STMTRS>
        <CURDEF>USD</CURDEF>
        <BANKACCTFROM>
          <BANKID>123456789</BANKID>
          <ACCTID>987654321</ACCTID>
          <ACCTTYPE>CHECKING</ACCTTYPE>
        </BANKACCTFROM>
        <BANKTRANLIST>
          <DTSTART>20260701000000</DTSTART>
          <DTEND>20260710000000</DTEND>
          <STMTTRN>
            <TRNTYPE>CREDIT</TRNTYPE>
            <DTPOSTED>20260702120000[-5:EST]</DTPOSTED>
            <TRNAMT>1500.00</TRNAMT>
            <FITID>2026070200001</FITID>
            <NAME>ACME CORP PAYMENT</NAME>
            <MEMO>INV-1001</MEMO>
          </STMTTRN>
          <STMTTRN>
            <TRNTYPE>DEBIT</TRNTYPE>
            <DTPOSTED>20260705</DTPOSTED>
            <TRNAMT>-25.00</TRNAMT>
            <FITID>2026070500001</FITID>
            <NAME>MONTHLY SERVICE FEE</NAME>
          </STMTTRN>
        </BANKTRANLIST>
        <LEDGERBAL>
          <BALAMT>1475.00</BALAMT>
          <DTASOF>20260710120000</DTASOF>
        </LEDGERBAL>
      </STMTRS>
    </STMTTRNRS>
  </BANKMSGSRSV1>
</OFX>
"""

# Same statement, minus the LEDGERBAL block entirely -- a bank that gives
# no balances at all (object_banking.py's tie_out "ran: False" case).
SGML_NO_LEDGERBAL = SGML_STATEMENT.replace(
    """<LEDGERBAL>
<BALAMT>1475.00
<DTASOF>20260710120000[-5:EST]
</LEDGERBAL>
""", "")

CC_STATEMENT = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<CREDITCARDMSGSRSV1>
<CCSTMTTRNRS>
<TRNUID>1
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<CCSTMTRS>
<CURDEF>USD
<CCACCTFROM>
<ACCTID>4111111111111111
</CCACCTFROM>
<BANKTRANLIST>
<DTSTART>20260701000000
<DTEND>20260710000000
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260703
<TRNAMT>-89.99
<FITID>CC0001
<NAME>ONLINE STORE
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>-89.99
<DTASOF>20260710
</LEDGERBAL>
</CCSTMTRS>
</CCSTMTTRNRS>
</CREDITCARDMSGSRSV1>
</OFX>
"""


def _min_sgml(stmttrn_body: str) -> str:
    """A minimal single-transaction OFX 1.x envelope, for pinning one
    behaviour (a date form, a NAME/MEMO combination) without repeating the
    whole header/account boilerplate each time."""
    return _min_sgml_multi(f"<STMTTRN>\n{stmttrn_body}\n</STMTTRN>")


def _min_sgml_multi(stmttrn_blocks: str) -> str:
    """Like _min_sgml, but takes one or more already-wrapped
    <STMTTRN>...</STMTTRN> blocks verbatim -- for tests that need more
    than one transaction in the same BANKTRANLIST (same-day twins)."""
    return f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>USD
<BANKACCTFROM>
<BANKID>123456789
<ACCTID>987654321
<ACCTTYPE>CHECKING
</BANKACCTFROM>
<BANKTRANLIST>
{stmttrn_blocks}
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""


def _strip_raw(lines):
    return [{k: v for k, v in line.items() if k != "raw"} for line in lines]


# --- 1. OFX 1.x SGML with unclosed tags ---------------------------------------

def test_sgml_statement_with_unclosed_tags_parses():
    result = object_ofx.parse_ofx(SGML_STATEMENT)
    lines = result["lines"]
    assert len(lines) == 2
    assert [l["posted_on"] for l in lines] == ["2026-07-02", "2026-07-05"]
    assert [l["amount_cents"] for l in lines] == [150000, -2500]
    assert result["account"] == {
        "account_id": "987654321", "bank_id": "123456789", "account_type": "CHECKING"}
    assert result["currency"] == "USD"
    # raw keeps the original transaction block verbatim -- the evidence.
    assert "1500.00" in lines[0]["raw"] and "ACME CORP" in lines[0]["raw"]


# --- 2. OFX 2.x XML parses to the same shape ----------------------------------

def test_xml_statement_parses_to_the_same_shape_as_sgml():
    sgml = object_ofx.parse_ofx(SGML_STATEMENT)
    xml = object_ofx.parse_ofx(XML_STATEMENT)
    assert _strip_raw(sgml["lines"]) == _strip_raw(xml["lines"])
    assert sgml["account"] == xml["account"]
    assert sgml["balances"] == xml["balances"]
    assert sgml["period"] == xml["period"]
    assert sgml["currency"] == xml["currency"]
    # XML's raw is a real closed-tag slice, distinct text from SGML's.
    assert "<TRNAMT>1500.00</TRNAMT>" in xml["lines"][0]["raw"]


def test_is_ofx_sniffs_both_dialects_and_rejects_garbage():
    assert object_ofx.is_ofx(SGML_STATEMENT) is True
    assert object_ofx.is_ofx(XML_STATEMENT) is True
    assert object_ofx.is_ofx("just some random text, not a statement") is False
    assert object_ofx.is_ofx("") is False


# --- 3. signed amounts, exact cents --------------------------------------------

def test_signed_amounts_to_exact_integer_cents():
    assert object_ofx.parse_ofx_amount("-25.00") == -2500
    assert object_ofx.parse_ofx_amount("1500.00") == 150000
    assert object_ofx.parse_ofx_amount("+42.10") == 4210
    assert object_ofx.parse_ofx_amount("0.00") == 0
    with pytest.raises(OfxError):
        object_ofx.parse_ofx_amount("")
    with pytest.raises(OfxError):
        object_ofx.parse_ofx_amount("not-a-number")


def test_deposit_positive_and_withdrawal_negative_in_a_real_statement():
    lines = object_ofx.parse_ofx(SGML_STATEMENT)["lines"]
    deposit, withdrawal = lines
    assert deposit["amount_cents"] == 150000
    assert withdrawal["amount_cents"] == -2500


# --- 4. FITID becomes external_id ---------------------------------------------

def test_fitid_becomes_external_id():
    lines = object_ofx.parse_ofx(SGML_STATEMENT)["lines"]
    assert lines[0]["external_id"] == "2026070200001"
    assert lines[1]["external_id"] == "2026070500001"


# --- 5. dates: full timestamp+tz and bare YYYYMMDD, unshifted -----------------

def test_full_timestamp_with_negative_tz_is_taken_as_printed():
    fixture = _min_sgml(
        "<DTPOSTED>20260702120000[-5:EST]\n<TRNAMT>10.00\n<FITID>X1\n<NAME>A")
    assert object_ofx.parse_ofx(fixture)["lines"][0]["posted_on"] == "2026-07-02"


def test_bare_yyyymmdd_date_parses_to_the_same_calendar_date():
    fixture = _min_sgml("<DTPOSTED>20260705\n<TRNAMT>10.00\n<FITID>X2\n<NAME>A")
    assert object_ofx.parse_ofx(fixture)["lines"][0]["posted_on"] == "2026-07-05"


def test_extreme_positive_tz_offset_near_midnight_does_not_shift_the_day():
    # A +13 offset on a midnight timestamp is exactly the case a naive
    # "convert to UTC" implementation would slide into the previous day.
    fixture = _min_sgml(
        "<DTPOSTED>20260101000000.500[+13:NZDT]\n<TRNAMT>10.00\n<FITID>X3\n<NAME>A")
    assert object_ofx.parse_ofx(fixture)["lines"][0]["posted_on"] == "2026-01-01"


def test_parse_ofx_date_rejects_unparseable_text():
    with pytest.raises(OfxError):
        object_ofx.parse_ofx_date("not-a-date")
    with pytest.raises(OfxError):
        object_ofx.parse_ofx_date("")


# --- 6. NAME/MEMO combination --------------------------------------------------

def test_name_and_memo_combine_when_both_present_and_different():
    fixture = _min_sgml(
        "<DTPOSTED>20260702\n<TRNAMT>10.00\n<FITID>X1\n"
        "<NAME>ACME CORP\n<MEMO>INV-1001")
    assert object_ofx.parse_ofx(fixture)["lines"][0]["description"] == "ACME CORP — INV-1001"


def test_name_alone_is_used_as_is():
    fixture = _min_sgml("<DTPOSTED>20260702\n<TRNAMT>10.00\n<FITID>X2\n<NAME>ACME CORP")
    assert object_ofx.parse_ofx(fixture)["lines"][0]["description"] == "ACME CORP"


def test_identical_name_and_memo_are_not_duplicated():
    fixture = _min_sgml(
        "<DTPOSTED>20260702\n<TRNAMT>10.00\n<FITID>X3\n"
        "<NAME>ACME CORP\n<MEMO>ACME CORP")
    assert object_ofx.parse_ofx(fixture)["lines"][0]["description"] == "ACME CORP"


def test_neither_name_nor_memo_is_tolerated_as_empty_description():
    fixture = _min_sgml("<DTPOSTED>20260702\n<TRNAMT>10.00\n<FITID>X4")
    result = object_ofx.parse_ofx(fixture)
    assert result["lines"][0]["description"] == ""


# --- 7. LEDGERBAL -> closing balance; absent -> None, not 0 ------------------

def test_ledgerbal_becomes_closing_balance_cents():
    balances = object_ofx.parse_ofx(SGML_STATEMENT)["balances"]
    assert balances["closing_balance_cents"] == 147500
    assert balances["as_of"] == "2026-07-10"
    # No standard OFX tag carries an opening balance -- honestly None.
    assert balances["opening_balance_cents"] is None


def test_missing_ledgerbal_yields_none_not_zero():
    balances = object_ofx.parse_ofx(SGML_NO_LEDGERBAL)["balances"]
    assert balances["closing_balance_cents"] is None
    assert balances["as_of"] == ""


# --- 8. credit-card (CCSTMTRS) statements --------------------------------------

def test_credit_card_statement_parses():
    result = object_ofx.parse_ofx(CC_STATEMENT)
    assert result["account"] == {
        "account_id": "4111111111111111", "bank_id": "", "account_type": "CREDITCARD"}
    assert len(result["lines"]) == 1
    line = result["lines"][0]
    assert line["posted_on"] == "2026-07-03"
    assert line["amount_cents"] == -8999
    assert line["external_id"] == "CC0001"
    assert result["balances"]["closing_balance_cents"] == -8999


# --- 9. malformed input refused; DOCTYPE/ENTITY refused ------------------------

def test_garbage_input_raises_ofxerror():
    with pytest.raises(OfxError):
        object_ofx.parse_ofx("this is not an OFX file, just some prose.\n")
    with pytest.raises(OfxError):
        object_ofx.parse_ofx("")


def test_truncated_xml_raises_ofxerror_not_a_parser_traceback():
    truncated = XML_STATEMENT.replace("</OFX>", "")  # unterminated root
    with pytest.raises(OfxError):
        object_ofx.parse_ofx(truncated)


def test_stmttrn_missing_dtposted_is_refused_not_half_read():
    fixture = _min_sgml("<TRNAMT>10.00\n<FITID>X5\n<NAME>NO DATE HERE")
    with pytest.raises(OfxError):
        object_ofx.parse_ofx(fixture)


def test_doctype_declaration_is_refused_before_parsing():
    attack = ('<?xml version="1.0"?>\n'
              '<!DOCTYPE ofx [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
              + XML_STATEMENT)
    with pytest.raises(OfxError):
        object_ofx.parse_ofx(attack)


def test_entity_declaration_without_doctype_wrapper_is_also_refused():
    # Belt and suspenders: refuse on the ENTITY token itself, not only when
    # it is nested inside a DOCTYPE block we happen to recognise.
    attack = "<!ENTITY xxe SYSTEM \"file:///etc/passwd\">\n" + SGML_STATEMENT
    with pytest.raises(OfxError):
        object_ofx.parse_ofx(attack)


def test_doctype_attack_on_the_sgml_dialect_is_also_refused():
    # The SGML branch never touches an XML parser at all, so it is not
    # actually vulnerable to entity expansion -- but a hostile DOCTYPE has
    # no legitimate place in a bank statement of either dialect, and the
    # refusal is applied uniformly before dialect detection even runs.
    attack = "<!DOCTYPE ofx [<!ENTITY xxe \"boom\">]>\n" + SGML_STATEMENT
    with pytest.raises(OfxError):
        object_ofx.parse_ofx(attack)


# --- 10. round-trip through object_banking.assign_line_hashes ----------------

def test_parsed_lines_get_stable_line_hashes_through_object_banking():
    parsed = object_ofx.parse_ofx(SGML_STATEMENT)
    stamped = object_banking.assign_line_hashes(parsed["lines"])
    assert all(line["line_hash"] for line in stamped)

    # Deterministic: hashing the same parsed lines again gives identical
    # hashes -- this is what makes file-level re-import a safe no-op.
    stamped_again = object_banking.assign_line_hashes(parsed["lines"])
    assert [l["line_hash"] for l in stamped] == [l["line_hash"] for l in stamped_again]

    # Pins the actual formula, not just "truthy": ordinal 0 for a line
    # with no same-day twin in this statement.
    first = stamped[0]
    expected = object_banking.line_hash(
        first["posted_on"], first["amount_cents"], first["description"], 0)
    assert first["line_hash"] == expected


def test_same_day_twin_lines_get_disambiguated_ordinals():
    # Two genuine same-day twins (identical date/amount/description, the
    # bank's own distinct FITIDs) in one BANKTRANLIST.
    twins = ("<STMTTRN>\n<DTPOSTED>20260702\n<TRNAMT>-4.50\n<FITID>T1\n<NAME>COFFEE\n</STMTTRN>\n"
             "<STMTTRN>\n<DTPOSTED>20260702\n<TRNAMT>-4.50\n<FITID>T2\n<NAME>COFFEE\n</STMTTRN>")
    parsed = object_ofx.parse_ofx(_min_sgml_multi(twins))
    assert len(parsed["lines"]) == 2
    stamped = object_banking.assign_line_hashes(parsed["lines"])
    assert stamped[0]["line_hash"] != stamped[1]["line_hash"]  # twins, still distinct
    assert stamped[0]["external_id"] != stamped[1]["external_id"]  # FITID already distinguishes them
