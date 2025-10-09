# utils_email.py
from typing import List, Tuple
from django.utils.timezone import localdate

def _has_email(addr: str | None) -> bool:
    return bool(addr and "@" in addr)

def _fmt_trip_dates(d) -> str:
    go = d.date.isoformat() if d.date else "未定"
    rt = d.return_date.isoformat() if d.return_date else "未定"
    return f"出發：{go}\n回程：{rt}"

def _fmt_passenger_block(p) -> str:
    pay = (f"願付：NT$ {p.willing_to_pay}" if p.willing_to_pay else "願付：未填")
    together = {
        True: "一起回程",
        False: "不一起回程",
        None: "未指定"
    }[p.together_return]
    return (
        f"乘客：{p.passenger_name}\n"
        f"性別：{getattr(p, 'get_gender_display', lambda: p.gender)() if hasattr(p,'get_gender_display') else p.gender}\n"
        f"人數：{p.seats_needed}\n"
        f"{pay}\n"
        f"上車：{p.departure or '未填'}\n"
        f"目的地：{p.destination or '未填'}\n"
        f"{_fmt_trip_dates(p)}\n"
        f"一起回程：{together}\n"
        f"聯絡方式：{p.contact or '（隱藏或未填）'}\n"
        f"Email：{p.email or '（隱藏或未填）'}\n"
        f"備註：{p.note or '無'}"
    )

def _fmt_driver_block(d) -> str:
    flex = {"YES":"順路可載","NO":"不順路也OK"}.get(d.flexible_pickup, "視情況")
    return (
        f"司機：{d.driver_name}\n"
        f"性別：{getattr(d, 'get_gender_display', lambda: d.gender)() if hasattr(d,'get_gender_display') else d.gender}\n"
        f"{_fmt_trip_dates(d)}\n"
        f"順路意願：{flex}\n"
        f"酌收費用：{d.fare_note or '未填'}\n"
        f"聯絡方式：{d.contact or '（隱藏或未填）'}\n"
        f"Email：{d.email or '（隱藏或未填）'}\n"
        f"備註：{d.note or '無'}"
    )

def build_join_emails(d, p) -> List[Tuple[str, str, list[str]]]:
    """
    根據 6 種情境，回傳要寄出的信件清單：
    [(subject, body, [to...]), ...]
    d: DriverTrip
    p: PassengerRequest
    """
    msgs: list[tuple[str,str,list[str]]] = []
    today = localdate().isoformat()

    driver_has_email = _has_email(getattr(d, "email", None))
    pax_has_email    = _has_email(getattr(p, "email", None))

    # 共同：通知用標題
    subject_join_notice = f"【新報名】{p.passenger_name} 報名了你的行程 - {today}"
    subject_join_full   = f"【新報名（含聯絡）】{p.passenger_name} 報名了你的行程 - {today}"
    subject_driver_info = f"【司機聯絡資訊】{d.driver_name} 的聯絡方式 - {today}"

    # 1) 乘客隱藏 + 自動寄信 + 司機有 Email → 把乘客報名資料寄給司機
    if p.hide_contact and p.auto_email_contact and driver_has_email:
        body = (
            "你收到一位新的乘客報名（已開啟自動寄信）：\n\n"
            f"{_fmt_passenger_block(p)}\n\n"
            "※ 此信由系統代送，請直接回覆乘客 Email 或使用其聯絡方式聯繫。"
        )
        msgs.append((subject_join_full, body, [d.email]))

    # 2) 乘客隱藏 + 關閉自動寄信 → 只寄通知給司機（不含乘客聯絡資料）
    if p.hide_contact and not p.auto_email_contact and driver_has_email:
        body = (
            "你收到一位乘客報名（乘客隱藏聯絡方式）：\n\n"
            f"乘客：{p.passenger_name}\n"
            f"人數：{p.seats_needed}\n"
            f"上車：{p.departure or '未填'} → 目的地：{p.destination or '未填'}\n"
            f"{_fmt_trip_dates(p)}\n\n"
            "※ 乘客未啟用自動寄信，請於系統中查看詳細資訊，並且已將您的聯絡資料寄送給乘客。"
        )
        msgs.append((subject_join_notice, body, [d.email]))

    # 3) 乘客未隱藏 → 直接把完整報名資料寄給司機
    if not p.hide_contact and driver_has_email:
        body = (
            "你收到一位乘客報名（乘客公開聯絡方式）：\n\n"
            f"{_fmt_passenger_block(p)}\n\n"
            "※ 請直接聯繫乘客或於系統中處理報名。"
        )
        msgs.append((subject_join_full, body, [d.email]))

    # 4) 司機隱藏 + 自動寄信 + 乘客有 Email → 把司機聯絡方式寄給乘客
    if getattr(d, "hide_contact", False) and getattr(d, "auto_email_contact", False) and pax_has_email:
        body = (
            "司機的聯絡方式如下（司機已啟用自動寄信）：\n\n"
            f"{_fmt_driver_block(d)}\n\n"
            "※ 此信由系統代送，請直接回覆司機 Email 或使用其聯絡方式聯繫。"
        )
        msgs.append((subject_driver_info, body, [p.email]))

    # 5) 司機隱藏 + 關閉自動寄信 → 不動作
    # 6) 司機未隱藏 → 不動作（因為已在 3) 把乘客資料寄給司機；司機資訊不需另外寄）

    return msgs
