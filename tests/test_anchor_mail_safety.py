"""隔离复现养号误判；网络和发信均由 fixture 替代。"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load(name, path=None):
    spec = importlib.util.spec_from_file_location(name, path or ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gmail = load("anchor_gmail_scan")


def mail(**headers):
    values = {"From": "buyer@example.test", "Subject": "Re: material", "In-Reply-To": "<sent@example.test>"}
    values.update(headers)
    return {"labelIds": ["INBOX"], "payload": {"headers": [{"name": k, "value": v} for k, v in values.items()]}}


@pytest.mark.parametrize("subject", [
    "Re: reworked storyboard after the walkthrough feedback - lemwarmup 0A3GN9JM",
    "RE: Fwd: reworked storyboard - LeMwArMuP 0a3gn9jm",
])
def test_incident_warmup_is_not_a_customer_reply(subject):
    assert not gmail.is_reply(mail(Subject=subject))


def test_old_sender_only_predicate_reproduces_incident():
    # 来自本次修复前旧脚本 67–73 行的最小判定，保留反例且不依赖生产文件。
    systems = ("google", "noreply", "no-reply", "payments", "dmarcreport", "dmarc",
               "github", "mailer-daemon", "mailinblack")
    frm = "StriveStays Other Emails <irtaza#@getstrive.sbs>"
    assert not any(s in frm.lower() for s in systems)
    assert "anchor-ceramicsfiber.com" not in frm


def test_normal_warm_words_are_not_blocked():
    assert gmail.is_reply(mail(Subject="Re: warm insulation for our workshop"))
