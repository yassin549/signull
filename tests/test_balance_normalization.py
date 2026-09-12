import pytest
from src.polymarket import _normalize_usdc_amount, _extract_allowance
from src.account import gas_payer_address


def test_normalize_usdc_amount_raw_micro_units():
    # 101,627 micro-USDC (6 decimals) = $0.101627 USDC
    assert _normalize_usdc_amount("101627") == 0.101627
    assert _normalize_usdc_amount(101627) == 0.101627
    assert _normalize_usdc_amount(101627.0) == 0.101627

    # 1,000,000 micro-USDC = $1.00 USDC
    assert _normalize_usdc_amount("1000000") == 1.0
    assert _normalize_usdc_amount(1000000) == 1.0

    # 0 micro-USDC = $0.00
    assert _normalize_usdc_amount("0") == 0.0
    assert _normalize_usdc_amount(0) == 0.0
    assert _normalize_usdc_amount(None) == 0.0

    # 50,000,000 micro-USDC = $50.00 USDC
    assert _normalize_usdc_amount("50000000") == 50.0

    # 25,000,000 micro-USDC = $25.00 USDC
    assert _normalize_usdc_amount("25000000") == 25.0
    assert _normalize_usdc_amount(25_000_000) == 25.0

    # Human formatted USDC strings with decimals (e.g. "25.5", 25.5)
    assert _normalize_usdc_amount("25.5") == 25.5
    assert _normalize_usdc_amount(25.5) == 25.5
    assert _normalize_usdc_amount(0.101627) == 0.101627


def test_extract_allowance_from_result():
    raw_result = {
        "balance": "101627",
        "allowances": {
            "0xE111180000d2663C0091e4f400237545B87B996B": "115792089237316195423570985008687907853269984665640564039457584007913129639935",
            "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296": "1000000",
        },
    }
    allow = _extract_allowance(raw_result)
    assert allow >= 1_000_000.0


def test_gas_payer_address():
    signer = "0x827412273bB2483324B726909f4Ea7cd4f0f6551"
    funder = "0xbC64E0fFFb940d3A305356c2DF2AD8F93b6745BD"

    # EOA (type 0)
    assert gas_payer_address(0, signer, funder) == signer
    # Deposit Wallet / 1271 (type 3)
    assert gas_payer_address(3, signer, funder) == funder
