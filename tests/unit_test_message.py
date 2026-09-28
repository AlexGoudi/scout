from scout_impl.mining.message import (
    BODY_MAX_CHARS,
    Redactor,
    author_id,
    is_bot,
    parse_message,
    split_message,
)


def parse(message, names=(), count_pulled=False, own=()):
    redactor = Redactor(names, stoplist=("github", "microsoft")).with_names(own)
    return parse_message(message, redact=redactor, count_pulled=count_pulled)


def test_split_message_joins_the_first_paragraph():
    assert split_message("\nfirst line\nsecond line\n\nbody\n") == ("first line second line", "body\n")


def test_subject_pr_number_and_bracket_tags():
    message = parse("[Mellanox] [SAI]  Update SDK (#1234)")
    assert message.pr_number == 1234
    assert message.subject_tags == ("mellanox", "sai")


def test_colon_tag_when_there_are_no_brackets():
    assert parse("orchagent: fix a leak (#5)").subject_tags == ("orchagent",)


def test_quoted_revert_with_trailer():
    message = parse('Revert "[dhcp] Fix syslog (#10)" (#12)\n\nThis reverts commit 0123456789abcdef.\n')
    assert (message.revert_depth, message.reverts_sha, message.reverts_pr) == (1, "0123456789abcdef", 10)
    assert message.pr_number == 12
    assert message.subject_tags == ("dhcp",)


def test_nested_revert_reports_depth_and_pr_of_the_reverted_change():
    message = parse('Revert "Revert "[bgp] add knob (#3)" (#4)" (#5)')
    assert message.revert_depth == 2
    assert message.reverts_pr == 4


def test_bare_revert_falls_back_to_a_pr_reference_in_the_body():
    message = parse("Revert frr upgrade\n\nReverts sonic-net/sonic-buildimage#77\n")
    assert (message.revert_depth, message.reverts_pr, message.reverts_sha) == (1, 77, None)


def test_unquoted_revert_ignores_its_own_pr_number():
    assert parse("Revert the serial watchdog (#1766)").reverts_pr is None
    assert parse('Revert PR#11831 (#12035) "Upgrade base image"').reverts_pr == 11831
    assert parse("Revert incorrect submodule changes in #13056 (#13262)").reverts_pr == 13056


def test_quoted_revert_with_several_trailing_pr_numbers():
    message = parse('Revert "[swss] drop dependency (#13084) (#14341)" (#15094) (#17367)')
    assert (message.revert_depth, message.reverts_pr, message.pr_number) == (1, 14341, 17367)
    assert message.subject_tags == ("swss",)


def test_not_a_revert():
    assert parse("Reverting behaviour is documented").revert_depth == 0
    assert parse("Add revert helper").revert_depth == 0


def test_template_sections_are_extracted():
    body = (
        "#### Why I did it\nBecause.\n\n##### Work item tracking\n- id\n\n"
        "#### How I did it\nCarefully.\n\n**- How to verify it**\nRan tests.\n\n"
        "#### Which release branch\n- [x] 202405\n"
    )
    message = parse("subject\n\n" + body)
    assert message.sections == {"why": "Because.", "how": "Carefully.", "verify": "Ran tests."}


def test_trailers_are_counted_and_removed():
    message = parse(
        "subject\n\nbody text\n\nSigned-off-by: Alice Example <alice@example.com>\n"
        "Co-authored-by: Bob <bob@example.com>\nReviewed-by: Carol <c@example.com>\n"
    )
    assert message.trailer_counts == {"signed_off_by": 1, "co_authored_by": 1, "other": 1}
    assert message.body == "body text"


def test_emails_and_universe_names_are_redacted():
    names = ["Joe LeVeque", "lguohan", "HP", "GitHub", "Marty Y. Lok"]
    message = parse(
        "Fix for joe  leveque\n\nThanks Joe LeVeque, lguohan and Marty Y. Lok <marty@x.org>.\n"
        "Lguohan stays, HP stays, GitHub stays.\n",
        names=names,
    )
    assert message.subject == "Fix for <name>"
    assert message.body == "Thanks <name>, <name> and <name> <<email>>.\nLguohan stays, HP stays, GitHub stays."


def test_possessive_and_bracketed_names_are_redacted():
    names = ["Barry Friedman", "Dante (Kuo-Jung) Su", "Gnanapriya [Marvell]", "lguohan", "Long Ou"]
    message = parse(
        "s\n\nBarry Friedman's PR, lguohan's fix, [Dante (Kuo-Jung) Su] and [Gnanapriya [Marvell]].\n"
        "long output stays, https://github.com/lguohan/switch does not.\n",
        names=names,
    )
    assert message.body == (
        "<name>'s PR, <name>'s fix, [<name>] and [<name>].\n"
        "long output stays, https://github.com/<name>/switch does not."
    )


def test_names_across_a_newline_are_not_joined():
    assert "Joe" in parse("s\n\nJoe\nLeVeque", names=["Joe LeVeque"]).body


def test_own_short_names_are_redacted_but_universe_short_names_are_not():
    assert parse("s\n\nVenu did it", own=["Venu"]).body == "<name> did it"
    assert parse("s\n\nVenu did it", names=["Venu"]).body == "Venu did it"


def test_redaction_digest_depends_on_the_universe_only():
    assert Redactor(["Alice Example"]).digest == Redactor(["Alice  Example"]).digest
    assert Redactor(["Alice Example"]).digest != Redactor(["Bob Builder"]).digest


def test_pulled_commit_lines_are_counted_and_author_tails_removed():
    body = (
        "#### Why I did it\nsrc/sonic-swss\n```\n"
        "* 35fb54fd - (HEAD -> master) [fdbsyncd]: Validate entries (#4847) (6 hours ago) [Alice Example]\n"
        "* b75a5a11 - Fix orchagent (#4830) (2 days ago) [Some Stranger]\n```\n"
    )
    message = parse("[submodule] Update sonic-swss\n\n" + body, count_pulled=True)
    assert message.pulled_commit_lines == 2
    assert "Stranger" not in message.body and "ago)" not in message.body
    assert parse("s\n\n" + body, count_pulled=False).pulled_commit_lines == 0


def test_body_is_capped_after_redaction():
    message = parse("s\n\n" + "Alice Example " * 5000, names=["Alice Example"])
    assert message.body_truncated
    assert len(message.body) == BODY_MAX_CHARS
    assert "Alice" not in message.body


def test_author_id_is_salted_and_normalized():
    assert author_id("Alice@Example.com ") == author_id("alice@example.com")
    assert author_id("alice@example.com", "a") != author_id("alice@example.com", "b")
    assert len(author_id("x@y.z")) == 16


def test_is_bot_matches_name_or_local_part():
    assert is_bot("mssonicbld", "sonicbld@microsoft.com", ["mssonicbld"])
    assert is_bot("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com", ["dependabot"])
    assert not is_bot("Alice", "alice@example.com", ["mssonicbld"])
