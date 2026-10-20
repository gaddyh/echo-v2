"""Thirteen compact Gold scenarios for direct Baseline B evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from echo_v2.domain.guard_simple import (
    ChildRole,
    SimpleConversationMessage,
)


@dataclass(frozen=True)
class SimpleGoldCheckpoint:
    after_message_id: str
    expected_parent_should_know: tuple[bool, ...]
    expected_child_roles: tuple[ChildRole, ...] = (
        ChildRole.PARTICIPANT,
        ChildRole.TARGET,
        ChildRole.BYSTANDER,
        ChildRole.UNKNOWN,
    )


@dataclass(frozen=True)
class SimpleGoldCase:
    case_id: str
    title: str
    chat_type: str
    messages: tuple[SimpleConversationMessage, ...]
    checkpoints: tuple[SimpleGoldCheckpoint, ...]


CASES: tuple[SimpleGoldCase, ...] = (
    SimpleGoldCase(
        case_id="simple_meetup_benign",
        title="Friends arranging a normal meetup",
        chat_type="private",
        messages=(
            SimpleConversationMessage("meet-1", "other", "Want to meet at the park after school?"),
            SimpleConversationMessage("meet-2", "child", "Sure, see you there."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint("meet-1", (False, None)),
            SimpleGoldCheckpoint("meet-2", (False,)),
        ),
    ),
    SimpleGoldCase(
        case_id="simple_mutual_teasing",
        title="Mutual teasing between friends",
        chat_type="private",
        messages=(
            SimpleConversationMessage("tease-1", "other", "You are so slow at this game 😂"),
            SimpleConversationMessage("tease-2", "child", "I still beat you 😂"),
            SimpleConversationMessage("tease-3", "other", "Rematch?"),
        ),
        checkpoints=tuple(
            SimpleGoldCheckpoint(message_id, (False,))
            for message_id in ("tease-1", "tease-2", "tease-3")
        ),
    ),
    SimpleGoldCase(
        case_id="simple_single_criticism",
        title="One criticism of the child",
        chat_type="group",
        messages=(
            SimpleConversationMessage("critic-1", "other", "Shay was annoying in class today."),
            SimpleConversationMessage("critic-2", "other_2", "Whatever, let's move on."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint(
                "critic-1", (False, None), (ChildRole.TARGET, ChildRole.UNKNOWN)
            ),
            SimpleGoldCheckpoint(
                "critic-2", (False, None), (ChildRole.TARGET, ChildRole.UNKNOWN)
            ),
        ),
    ),
    SimpleGoldCase(
        case_id="simple_repeated_group_targeting",
        title="Repeated group targeting of the child",
        chat_type="group",
        messages=(
            SimpleConversationMessage("bully-1", "other", "Shay is useless."),
            SimpleConversationMessage("bully-2", "other_2", "Nobody wants Shay here."),
            SimpleConversationMessage("bully-3", "other_3", "Everyone laugh at Shay again."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint("bully-1", (False, None)),
            SimpleGoldCheckpoint(
                "bully-2", (None, True), (ChildRole.TARGET,)
            ),
            SimpleGoldCheckpoint("bully-3", (True,), (ChildRole.TARGET,)),
        ),
    ),
    SimpleGoldCase(
        case_id="simple_secret_meeting_pressure",
        title="Pressure to meet secretly",
        chat_type="private",
        messages=(
            SimpleConversationMessage("secret-1", "other", "Don't tell your parents about this."),
            SimpleConversationMessage("secret-2", "child", "Why not?"),
            SimpleConversationMessage("secret-3", "other", "Meet me alone tonight or I'll be angry."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint("secret-1", (None, True)),
            SimpleGoldCheckpoint("secret-2", (True, None)),
            SimpleGoldCheckpoint("secret-3", (True,)),
        ),
    ),
    SimpleGoldCase(
        case_id="simple_credible_immediate_threat",
        title="Explicit credible threat of immediate harm",
        chat_type="private",
        messages=(
            SimpleConversationMessage("threat-1", "other", "If you tell anyone, you will regret it."),
            SimpleConversationMessage("threat-2", "child", "Stop threatening me."),
            SimpleConversationMessage("threat-3", "other", "I will be waiting outside your school tomorrow."),
            SimpleConversationMessage("threat-4", "other", "I know which bus you take home."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint("threat-1", (True, None)),
            SimpleGoldCheckpoint("threat-2", (True,)),
            SimpleGoldCheckpoint("threat-3", (True, False)),
            SimpleGoldCheckpoint("threat-4", (True,)),
        ),
    ),
    # Child-as-aggressor family (ported from behavior_cases.json). Harmful
    # conduct by the monitored child toward others is out of scope for
    # parent_should_know, so every checkpoint expects False.
    SimpleGoldCase(
        case_id="behavior_direct_threat",
        title="Monitored child threatens another student",
        chat_type="private",
        messages=(
            SimpleConversationMessage("m1", "other", "אני לא רוצה לריב איתך, די."),
            SimpleConversationMessage("m2", "child", "אם תגיד למחנכת מה קרה אני אחכה לך מחוץ לבית ספר."),
            SimpleConversationMessage("m3", "other", "אתה מאיים עליי?"),
            SimpleConversationMessage("m4", "child", "כן. כדאי לך לשתוק."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint("m1", (False,), (ChildRole.PARTICIPANT, ChildRole.UNKNOWN)),
            SimpleGoldCheckpoint("m2", (False,), (ChildRole.PARTICIPANT,)),
            SimpleGoldCheckpoint("m3", (False,), (ChildRole.PARTICIPANT,)),
            SimpleGoldCheckpoint("m4", (False,), (ChildRole.PARTICIPANT,)),
        ),
    ),
    SimpleGoldCase(
        case_id="behavior_coordinated_exclusion",
        title="Monitored child organizes a group boycott",
        chat_type="group",
        messages=(
            SimpleConversationMessage("m1", "child", "פותחים קבוצה חדשה בלי נועם. אל תוסיפו אותו."),
            SimpleConversationMessage("m2", "other", "למה? מה הוא עשה?"),
            SimpleConversationMessage("m3", "child", "מי שמדבר איתו שלא יבוא לשבת איתנו בהפסקה."),
            SimpleConversationMessage("m4", "other_2", "זה כבר ממש לא יפה."),
        ),
        checkpoints=tuple(
            SimpleGoldCheckpoint(message_id, (False,), (ChildRole.PARTICIPANT,))
            for message_id in ("m1", "m2", "m3", "m4")
        ),
    ),
    SimpleGoldCase(
        case_id="behavior_repeated_humiliation",
        title="Monitored child repeatedly humiliates another child",
        chat_type="group",
        messages=(
            SimpleConversationMessage("m1", "child", "תראו איך דניאל נפל היום, איזה אפס 😂"),
            SimpleConversationMessage("m2", "other", "שי די זה מביך אותי."),
            SimpleConversationMessage("m3", "child", "חכו אני מעלה גם את הסרטון שלו עוד פעם."),
            SimpleConversationMessage("m4", "other_2", "די שי, הוא ביקש שתפסיק."),
            SimpleConversationMessage("m5", "child", "שכולם יראו כמה הוא פתטי."),
        ),
        checkpoints=tuple(
            SimpleGoldCheckpoint(message_id, (False,), (ChildRole.PARTICIPANT,))
            for message_id in ("m1", "m2", "m3", "m4", "m5")
        ),
    ),
    SimpleGoldCase(
        case_id="behavior_private_image_threat",
        title="Monitored child threatens to spread an embarrassing photo",
        chat_type="private",
        messages=(
            SimpleConversationMessage("m1", "other", "בבקשה תמחק את התמונה שלי, שלחתי לך בפרטי."),
            SimpleConversationMessage("m2", "child", "אם לא תעשה מה שאני אומר אני שולח אותה לקבוצת הכיתה."),
            SimpleConversationMessage("m3", "other", "בבקשה אל תעשה את זה."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint("m1", (False,), (ChildRole.PARTICIPANT, ChildRole.UNKNOWN)),
            SimpleGoldCheckpoint("m2", (False,), (ChildRole.PARTICIPANT,)),
            SimpleGoldCheckpoint("m3", (False,), (ChildRole.PARTICIPANT,)),
        ),
    ),
    SimpleGoldCase(
        case_id="behavior_mutual_teasing_negative",
        title="Mutual teasing between friends",
        chat_type="private",
        messages=(
            SimpleConversationMessage("m1", "child", "איזה גרוע אתה בפיפא 😂"),
            SimpleConversationMessage("m2", "other", "חחח אתה מדבר? הפסדת לי אתמול 5-0"),
            SimpleConversationMessage("m3", "child", "יאללה משחק חוזר הערב?"),
            SimpleConversationMessage("m4", "other", "ברור יא תותח 😂"),
        ),
        checkpoints=tuple(
            SimpleGoldCheckpoint(message_id, (False,), (ChildRole.PARTICIPANT,))
            for message_id in ("m1", "m2", "m3", "m4")
        ),
    ),
    SimpleGoldCase(
        case_id="behavior_single_argument_negative",
        title="One-off argument",
        chat_type="group",
        messages=(
            SimpleConversationMessage("m1", "other", "שי, שכחת שוב להביא את העבודה."),
            SimpleConversationMessage("m2", "child", "די כבר לחפור לי, זה מעצבן."),
            SimpleConversationMessage("m3", "other", "טוב, נדבר מחר."),
            SimpleConversationMessage("m4", "child", "סבבה סליחה שהתעצבנתי."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint("m1", (False,)),
            SimpleGoldCheckpoint("m2", (False,), (ChildRole.PARTICIPANT,)),
            SimpleGoldCheckpoint("m3", (False,), (ChildRole.PARTICIPANT,)),
            SimpleGoldCheckpoint("m4", (False,), (ChildRole.PARTICIPANT,)),
        ),
    ),
    SimpleGoldCase(
        case_id="behavior_defensive_response_negative",
        title="Monitored child defends themselves against targeting",
        chat_type="group",
        messages=(
            SimpleConversationMessage("m1", "other", "שי אף אחד לא רוצה אותך בקבוצה, תעוף."),
            SimpleConversationMessage("m2", "other_2", "נכון, אף אחד לא סובל אותו."),
            SimpleConversationMessage("m3", "child", "עזבו אותי כבר, אתם ממש מגעילים."),
            SimpleConversationMessage("m4", "child", "תפסיקו לדבר אליי ככה."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint(
                "m1", (False, None, True), (ChildRole.TARGET, ChildRole.UNKNOWN)
            ),
            SimpleGoldCheckpoint("m2", (None, True), (ChildRole.TARGET,)),
            SimpleGoldCheckpoint("m3", (True,), (ChildRole.TARGET,)),
            SimpleGoldCheckpoint("m4", (True,), (ChildRole.TARGET,)),
        ),
    ),
)


def validate_cases(cases: tuple[SimpleGoldCase, ...] = CASES) -> None:
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Baseline B Gold case IDs must be unique")
    for case in cases:
        message_ids = [message.id for message in case.messages]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError(f"{case.case_id}: message IDs must be unique")
        positions = {message_id: index for index, message_id in enumerate(message_ids)}
        previous = -1
        for checkpoint in case.checkpoints:
            if checkpoint.after_message_id not in positions:
                raise ValueError(f"{case.case_id}: checkpoint message is unknown")
            current = positions[checkpoint.after_message_id]
            if current <= previous:
                raise ValueError(f"{case.case_id}: checkpoints must be chronological")
            previous = current


__all__ = ["CASES", "SimpleGoldCase", "SimpleGoldCheckpoint", "validate_cases"]
