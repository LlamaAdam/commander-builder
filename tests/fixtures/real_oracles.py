"""Curated byte-exact Scryfall oracle text fixtures.

## Why this exists

The 2026-05-14 chrome-audit follow-up caught **nine** classifier
bugs in ``staples.classify_role`` / ``classify_role_extended``
that all passed the existing unit tests. The unit tests used
hand-written synthetic oracle text that happened to match the
overly-permissive regex patterns; real Scryfall data exposed the
gaps:

- ``Cyclonic Rift`` — oracle has ``\\n`` between target clause and
  ``Overload {`` paragraph; original regex used ``.*`` (not
  ``[\\s\\S]``) and didn't cross newlines.
- ``Crux of Fate`` — real text is ``Destroy all Dragon creatures``
  (typed all-sweep), not ``Destroy each Dragon`` the test assumed.
- ``Coalition Victory`` — uses ``You win the game`` idiom that
  wasn't in ``_WIN_CONDITION_PATTERNS`` at all.
- ``Three Visits`` — ``Search your library for a Forest card``;
  pattern required literal word "land".
- ``Sylvan Library`` — ``draw two additional cards`` template; the
  "additional" qualifier broke the literal-word-order pattern.
- ``Toxic Deluge`` — ``All creatures get -X/-X``; no existing
  pattern matched the mass-shrink wipe shape.
- ``Mystical Tutor`` — ``instant or sorcery card``; flat
  alternation didn't tolerate the OR templating.
- ``Craterhoof Behemoth`` — real oracle says ``gain trample and
  get +X/+X``, OPPOSITE word order from the original pattern.
- Multiple cards had similar issues that the synthetic-text test
  fixtures hid.

## The rule

When writing a classification test, **always source the oracle
text from this module** rather than hand-writing a synthetic
approximation. Every value here was copy-pasted directly from a
live Scryfall API response (`https://api.scryfall.com/cards/named`),
including the ``\\n`` paragraph breaks and the typographic dashes.

## How to add a new card

1. Look up the card at ``https://scryfall.com/search?q=!"<name>"`` and
   copy the oracle text exactly as displayed.
2. Add it below in alphabetical-by-card-name order with a short
   comment explaining what role-classifier behavior the fixture
   pins.
3. Reference it in your test via
   ``from tests.fixtures.real_oracles import ORACLES``
   then ``ORACLES["Card Name"]``.

Do NOT paraphrase, normalize, or "clean up" the text. Real Scryfall
data has em-dashes, bullet glyphs, and trailing newlines that
matter — the classifier must handle them as Scryfall ships them.

## When Scryfall is unreachable (added 2026-08-20)

Some sessions run in a sandbox whose egress policy blocks
``api.scryfall.com`` outright (403 at the proxy) and whose local
``.cache/scryfall`` snapshot directory is empty — the same
constraint the politics tests already record
(tests/test_politics_guard.py's provenance note). The round-2
review fixes (R2-P10 / R2-P11) needed fixtures in exactly such a
session. The rule for that case, and ONLY that case:

1. Transcribe the printed oracle text. Never invent templating and
   never write "close enough" text that merely satisfies the regex
   under test — that is the synthetic-fixture failure mode this
   module exists to prevent.
2. Mark the entry ``PROVENANCE: offline transcription <date>``,
   naming what could not be verified. An unmarked entry is a claim
   of byte-exactness; an unmarked-but-unverified entry is worse
   than no fixture, because it makes a pattern LOOK pinned by real
   data.
3. Re-verify marked entries in the next session with network and
   drop the marker once the API confirms them.
"""

from __future__ import annotations


# Card name → ``{"oracle_text": str, "type_line": str}`` dict, sourced
# verbatim from Scryfall. Keys sorted alphabetically by card name.
ORACLES: dict[str, dict[str, str]] = {
    # Animate Dead — the canonical graveyard-aura reanimation spell.
    # Its return clause lives inside an errata replacement paragraph;
    # the stable signature is the "Enchant creature card in a
    # graveyard" line. Pins combo_detection._REANIMATION_PATTERNS'
    # aura shape (round-2 bracket-floor pass: reanimation-aware combo
    # speed pricing).
    "Animate Dead": {
        "oracle_text": (
            "Enchant creature card in a graveyard\n"
            "When this aura enters, if it's on the battlefield, it "
            "loses \"enchant creature card in a graveyard\" and gains "
            "\"enchant creature put onto the battlefield with this "
            "aura.\" Return enchanted creature card to the battlefield "
            "under your control and attach this aura to it. When this "
            "aura leaves the battlefield, that creature's controller "
            "sacrifices it.\n"
            "Enchanted creature gets -1/-0."
        ),
        "type_line": "Enchantment — Aura",
    },

    # Arcane Signet — natural-language "Add one mana of any color"
    # template that didn't match the classifier's strict
    # ``add \{[wubrgc]\}`` regex. Live-audit follow-up 2026-05-16
    # caught it falling through to "other"; the test_staples
    # ramp-detection test used a fake-but-regex-friendly variant
    # ("Add {W} or {U}.") as a workaround. Real oracle pinned here
    # so the regex stays honest going forward.
    "Arcane Signet": {
        "oracle_text": (
            "{T}: Add one mana of any color in your commander's "
            "color identity."
        ),
        "type_line": "Artifact",
    },

    # Ashnod's Altar — NEGATIVE guard for the round-2 edict pattern
    # (2026-08-16): sacrificing YOUR OWN creature as an activation
    # cost must never read as edict removal. Classifies as ramp via
    # its "Add {C}{C}" clause.
    "Ashnod's Altar": {
        "oracle_text": "Sacrifice a creature: Add {C}{C}.",
        "type_line": "Artifact",
    },

    # Big Score — plural-Treasure producer whose DRAW clause must
    # keep winning the role (draw 70 > treasure-ramp 40). Control
    # fixture for the round-2 treasure-plural ramp pattern.
    "Big Score": {
        "oracle_text": (
            "As an additional cost to cast this spell, discard a "
            "card.\n"
            "Draw two cards and create two Treasure tokens."
        ),
        "type_line": "Instant",
    },

    # Bloodbraid Elf — NEGATIVE guard for the round-2 impulse-draw
    # pattern: cascade's reminder text ("exile cards from the top of
    # your library ... You may cast it without paying its mana cost")
    # must NOT read as impulse draw. Classifies as threat (creature,
    # no stronger signal).
    "Bloodbraid Elf": {
        "oracle_text": (
            "Cascade (When you cast this spell, exile cards from the "
            "top of your library until you exile a nonland card that "
            "costs less. You may cast it without paying its mana "
            "cost. Put the exiled cards on the bottom of your "
            "library in a random order.)\n"
            "Haste"
        ),
        "type_line": "Creature — Elf Berserker",
    },

    # Chain Reaction — "deals X damage to each creature" scaling
    # sweep. The wipe pattern required literal digits until the
    # round-2 evergreen-gaps fix (2026-08-16).
    "Chain Reaction": {
        "oracle_text": (
            "Chain Reaction deals X damage to each creature, where X "
            "is the number of creatures on the battlefield."
        ),
        "type_line": "Sorcery",
    },

    # Coalition Victory — uses "You win the game" idiom which the
    # original ``_WIN_CONDITION_PATTERNS`` didn't cover. Pinned in
    # commit b2ff2b9.
    "Coalition Victory": {
        "oracle_text": (
            "You win the game if you control a land of each basic "
            "land type and a creature of each color."
        ),
        "type_line": "Sorcery",
    },

    # Corpse Dance — "Return the top creature card OF your graveyard"
    # (of, not from) — pins the from|of alternation in
    # combo_detection's reanimation return-clause pattern.
    "Corpse Dance": {
        "oracle_text": (
            "Buyback {2} (You may pay an additional {2} as you cast "
            "this spell. If you do, put this card into your hand as "
            "it resolves.)\n"
            "Return the top creature card of your graveyard to the "
            "battlefield. That creature gains haste. Exile it at the "
            "beginning of the next end step."
        ),
        "type_line": "Instant",
    },

    # Craterhoof Behemoth — "gain trample and get +X/+X" is the
    # OPPOSITE word order from the original
    # ``_WIN_CONDITION_PATTERNS`` entry ("get +N/+N and gain
    # trample"). Pinned in commit 085c256.
    "Craterhoof Behemoth": {
        "oracle_text": (
            "Haste\n"
            "When this creature enters, creatures you control gain "
            "trample and get +X/+X until end of turn, where X is the "
            "number of creatures you control."
        ),
        "type_line": "Creature — Beast",
    },

    # Crux of Fate — typed all-sweep ("destroy all Dragon creatures"),
    # not the "each <type>" idiom the original test fixture assumed.
    # Multi-paragraph with em-dash bullets. Pinned in commit b2ff2b9.
    "Crux of Fate": {
        "oracle_text": (
            "Choose one —\n"
            "• Destroy all Dragon creatures.\n"
            "• Destroy all non-Dragon creatures."
        ),
        "type_line": "Sorcery",
    },

    # Cyclonic Rift — the canonical overload bounce wipe. Real
    # Scryfall oracle has ``\n`` between the target clause and the
    # ``Overload {`` paragraph; the original regex used ``.*`` which
    # Python's ``re.search`` doesn't cross newlines without DOTALL.
    # Pinned in commit b2ff2b9.
    "Cyclonic Rift": {
        "oracle_text": (
            "Return target nonland permanent you don't control to "
            "its owner's hand.\n"
            "Overload {6}{U} (You may cast this spell for its "
            "overload cost. If you do, change \"target\" in its "
            "text to \"each.\")"
        ),
        "type_line": "Instant",
    },

    # Damnation — basic destroy-all template. Already classified
    # correctly before the audit but kept here as a control value
    # for the multi-paragraph parser.
    "Damnation": {
        "oracle_text": "Destroy all creatures. They can't be regenerated.",
        "type_line": "Sorcery",
    },

    # Dance of the Dead — second graveyard-aura reanimation shape;
    # like Animate Dead the reliable signature is the enchant line
    # ("Put enchanted creature card onto the battlefield" appears only
    # inside the errata paragraph).
    "Dance of the Dead": {
        "oracle_text": (
            "Enchant creature card in a graveyard\n"
            "When this aura enters, if it's on the battlefield, it "
            "loses \"enchant creature card in a graveyard\" and gains "
            "\"enchant creature put onto the battlefield with this "
            "aura.\" Put enchanted creature card onto the battlefield "
            "tapped under your control and attach this aura to it. "
            "When this aura leaves the battlefield, that creature's "
            "controller sacrifices it.\n"
            "Enchanted creature gets +1/+1 and doesn't untap during "
            "its controller's untap step.\n"
            "At the beginning of the upkeep of enchanted creature's "
            "controller, that player may pay {1}{B}. If the player "
            "does, untap that creature."
        ),
        "type_line": "Enchantment — Aura",
    },

    # Diabolic Edict — the class-defining edict: "Target player
    # sacrifices a creature" answers hexproof/shroud threats that
    # targeted removal can't touch. Classified "other" until the
    # round-2 evergreen-gaps fix (2026-08-16).
    "Diabolic Edict": {
        "oracle_text": "Target player sacrifices a creature of their choice.",
        "type_line": "Instant",
    },

    # Dockside Extortionist — "create X Treasure tokens" (no Treasure
    # reminder text in its oracle, so nothing else for the ramp
    # patterns to latch onto). The singular "create a treasure token"
    # pattern missed every plural/variable Treasure producer until
    # the round-2 fix.
    "Dockside Extortionist": {
        "oracle_text": (
            "When this creature enters, create X Treasure tokens, "
            "where X is the number of artifacts and enchantments "
            "your opponents control."
        ),
        "type_line": "Creature — Goblin Pirate",
    },

    # Dovin's Veto — restricted counterspell with a leading
    # can't-be-countered rider on its own paragraph; the "counter
    # target noncreature spell" clause is the load-bearing line.
    "Dovin's Veto": {
        "oracle_text": (
            "This spell can't be countered.\n"
            "Counter target noncreature spell."
        ),
        "type_line": "Instant",
    },

    # Earthquake — X-damage sweep ("deals X damage to each creature
    # without flying and each player"). The original wipe pattern
    # required literal digits, so every X-wipe classified "other".
    "Earthquake": {
        "oracle_text": (
            "Earthquake deals X damage to each creature without "
            "flying and each player."
        ),
        "type_line": "Sorcery",
    },

    # Eternal Witness — NEGATIVE control for the reanimation patterns:
    # graveyard recursion to HAND, not to the battlefield. Must not
    # classify as a reanimation spell.
    "Eternal Witness": {
        "oracle_text": (
            "When this creature enters, you may return target card "
            "from your graveyard to your hand."
        ),
        "type_line": "Creature — Human Shaman",
    },

    # Light Up the Stage — impulse draw with a spectacle rider. The
    # exile-to-play clause sits on its own paragraph AFTER the
    # keyword line; the spectacle reminder text contains "You may
    # cast this spell" and must not be what the impulse pattern
    # matches on. Round-2 evergreen-gaps fix (2026-08-16).
    "Light Up the Stage": {
        "oracle_text": (
            "Spectacle {R} (You may cast this spell for its "
            "spectacle cost rather than its mana cost if an opponent "
            "lost life this turn.)\n"
            "Exile the top two cards of your library. Until the end "
            "of your next turn, you may play those cards."
        ),
        "type_line": "Sorcery",
    },

    # Miirym, Sentinel Wyrm — ward in a comma-separated keyword line
    # ("Flying, vigilance, ward {2}"). The ward keyword postdated the
    # protection patterns entirely until the round-2 fix.
    "Miirym, Sentinel Wyrm": {
        "oracle_text": (
            "Flying, vigilance, ward {2}\n"
            "Whenever another nontoken Dragon enters the battlefield "
            "under your control, create a token that's a copy of it."
        ),
        "type_line": "Legendary Creature — Dragon Spirit",
    },

    # Mystical Tutor — "instant or sorcery card" OR-templating
    # pattern; flat alternation in the original tutor regex didn't
    # match. Pinned in commit 085c256.
    "Mystical Tutor": {
        "oracle_text": (
            "Search your library for an instant or sorcery card, "
            "reveal it, then shuffle and put that card on top of "
            "your library."
        ),
        "type_line": "Instant",
    },

    # Necromancy — "Put target creature card from a graveyard onto the
    # battlefield" mid-paragraph (after the flash rider) — pins that
    # the put-shape pattern doesn't require sentence-initial position.
    "Necromancy": {
        "oracle_text": (
            "You may cast this spell as though it had flash. If you "
            "cast it any time a sorcery couldn't have been cast, the "
            "controller of the permanent it becomes sacrifices it at "
            "the beginning of the next cleanup step.\n"
            "When this enchantment enters, if it's on the battlefield, "
            "it becomes an Aura with \"enchant creature put onto the "
            "battlefield with this enchantment.\" Put target creature "
            "card from a graveyard onto the battlefield under your "
            "control and attach this enchantment to it. When this "
            "enchantment leaves the battlefield, that creature's "
            "controller sacrifices it."
        ),
        "type_line": "Enchantment",
    },

    # Negate — the minimal restricted counterspell ("counter target
    # noncreature spell"). The original pattern required "spell"
    # immediately after "target", so Negate classified "other".
    "Negate": {
        "oracle_text": "Counter target noncreature spell.",
        "type_line": "Instant",
    },

    # Persist — the plain modern return-to-battlefield template with a
    # qualifier ("non-legendary") between "target" and "creature card".
    "Persist": {
        "oracle_text": (
            "Return target non-legendary creature card from your "
            "graveyard to the battlefield."
        ),
        "type_line": "Sorcery",
    },

    # Phyrexian Fleshgorger — "Ward—Pay ..." em-dash cost form (no
    # braces), the second ward templating the round-2 protection
    # pattern must catch.
    "Phyrexian Fleshgorger": {
        "oracle_text": (
            "Prototype {1}{B}{B} — 3/3 (You may cast this spell with "
            "different mana cost, color, and size. It keeps its "
            "abilities and types.)\n"
            "Menace, lifelink\n"
            "Ward—Pay life equal to this creature's power."
        ),
        "type_line": "Artifact Creature — Phyrexian Wurm",
    },

    # Prey Upon — the class-defining fight spell. Its reminder text
    # ("Each deals damage equal to its power to the other.") must not
    # be the clause that matches — "the other" is not "target
    # creature"; the fight clause itself is the signal.
    "Prey Upon": {
        "oracle_text": (
            "Target creature you control fights target creature you "
            "don't control. (Each deals damage equal to its power to "
            "the other.)"
        ),
        "type_line": "Sorcery",
    },

    # Prosper, Tome-Bound — engine-style impulse draw, singular form
    # ("exile the top card of your library. Until the end of your
    # next turn, you may play that card"). Also creates Treasures, so
    # it pins impulse-draw (60) outranking treasure-ramp (40).
    "Prosper, Tome-Bound": {
        "oracle_text": (
            "Deathtouch\n"
            "Mysterious Stranger — At the beginning of your end "
            "step, exile the top card of your library. Until the end "
            "of your next turn, you may play that card.\n"
            "Pact Boon — Whenever you play a card from exile, create "
            "a Treasure token."
        ),
        "type_line": "Legendary Creature — Tiefling Warlock",
    },

    # Ram Through — the class-defining bite spell ("deals damage
    # equal to its power to target creature"): one-sided fight,
    # no "fights" keyword anywhere in the text.
    "Ram Through": {
        "oracle_text": (
            "Target creature you control deals damage equal to its "
            "power to target creature you don't control. If the "
            "creature you control has trample, excess damage is "
            "dealt to that creature's controller instead."
        ),
        "type_line": "Instant",
    },

    # Reanimate — the minimal put-shape template.
    "Reanimate": {
        "oracle_text": (
            "Put target creature card from a graveyard onto the "
            "battlefield under your control. You lose life equal to "
            "its mana value."
        ),
        "type_line": "Sorcery",
    },

    # Smothering Tithe — the punisher-tax template the politics guard
    # missed until 2026-08-20 (R2-P10). The offer and its consequence sit
    # in TWO sentences ("that player may pay {2}. If the player doesn't,
    # ...") with no "unless" anywhere, so the original
    # ``\bunless that player pays\b`` pattern returned no tags for the
    # most-played tax in the format — the card the guard's own comment
    # named as covered. Classifies as ``ramp`` (the Treasure clause); the
    # politics tag is orthogonal to the role, which is the point: the
    # shield is a cut exemption, not a score.
    #
    # PROVENANCE: offline transcription 2026-08-20 — api.scryfall.com is
    # blocked by this sandbox's egress policy (403 at the proxy) and
    # .cache/scryfall is empty. Treasure tokens carry no reminder text in
    # Scryfall's oracle (cross-checked against the Dockside Extortionist
    # and Big Score entries above, both live-sourced), so this body is
    # believed complete; re-verify when network is available.
    "Smothering Tithe": {
        "oracle_text": (
            "Whenever an opponent draws a card, that player may pay {2}. "
            "If the player doesn't, you create a Treasure token."
        ),
        "type_line": "Enchantment",
    },

    # Soul Shatter — modern each-opponent edict wording ("Each
    # opponent sacrifices a creature or planeswalker with the
    # highest mana value ...").
    "Soul Shatter": {
        "oracle_text": (
            "Each opponent sacrifices a creature or planeswalker "
            "with the highest mana value among creatures and "
            "planeswalkers they control."
        ),
        "type_line": "Instant",
    },

    # Spell Pierce — restricted counterspell in the
    # unless-controller-pays form; both qualifiers at once
    # ("noncreature" + "unless its controller pays {2}").
    "Spell Pierce": {
        "oracle_text": (
            "Counter target noncreature spell unless its controller "
            "pays {2}."
        ),
        "type_line": "Instant",
    },

    # Sun Titan — NEGATIVE control: returns "permanent card with mana
    # value 2 or less", not "creature card" — recursion, but not the
    # reanimation shape the combo-speed rule reprices on.
    "Sun Titan": {
        "oracle_text": (
            "Vigilance\n"
            "Whenever this creature enters or attacks, you may return "
            "target permanent card with mana value 2 or less from "
            "your graveyard to the battlefield."
        ),
        "type_line": "Creature — Giant",
    },

    # Swan Song — the widest restricted-counter type list ("counter
    # target enchantment, instant, or sorcery spell"), with a
    # token-gift rider that must not distract the classifier.
    "Swan Song": {
        "oracle_text": (
            "Counter target enchantment, instant, or sorcery spell. "
            "Its controller creates a 2/2 blue Bird creature token "
            "with flying."
        ),
        "type_line": "Instant",
    },

    # Sylvan Library — "draw two additional cards"; the "additional"
    # qualifier between number and "cards" broke the literal-pattern
    # match. Pinned in commit 085c256.
    "Sylvan Library": {
        "oracle_text": (
            "At the beginning of your draw step, you may draw two "
            "additional cards. If you do, choose two cards in your "
            "hand drawn this turn. For each of those cards, pay 4 "
            "life or put the card on top of your library."
        ),
        "type_line": "Enchantment",
    },

    # Take Up the Shield — the shield-counter protection template
    # (R2-P11, 2026-08-20). Its ONLY protection signal is
    # "Put a shield counter on it": the +2/+2 and lifelink riders match
    # nothing in the role table, so this fixture isolates the new pattern
    # — if the shield-counter regex regresses, this entry drops to
    # "other" and the parametrized fixture test names the card.
    #
    # PROVENANCE: offline transcription 2026-08-20 (see the module
    # docstring). Shield counters carry a reminder paragraph on some
    # printings; it is omitted here because it could not be verified, and
    # the pattern is sentence-local so its presence would not change the
    # classification. Re-verify with network.
    "Take Up the Shield": {
        "oracle_text": (
            "Target creature gets +2/+2 and gains lifelink until end of "
            "turn. Put a shield counter on it."
        ),
        "type_line": "Instant",
    },

    # Teferi's Protection — the phasing protection template (R2-P11,
    # 2026-08-20). NOTE it also matches "protection from", so its
    # classification alone does NOT prove the phasing pattern fires;
    # test_staples.py asserts the phasing regex against this text
    # directly for that reason. Kept as the phasing fixture anyway
    # because it is the format's canonical phase-out card and pins that
    # the two protection patterns coexist without either shadowing the
    # role.
    #
    # PROVENANCE: offline transcription 2026-08-20 (see the module
    # docstring). The parenthetical phasing reminder printed on the C17
    # card is omitted because its exact wording could not be verified;
    # the load-bearing sentences are transcribed as printed. Re-verify
    # with network before using this entry for any paragraph-crossing
    # pattern.
    "Teferi's Protection": {
        "oracle_text": (
            "Until your next turn, your life total can't change and you "
            "have protection from everything. All permanents you control "
            "phase out."
        ),
        "type_line": "Instant",
    },

    # Three Visits — "Search your library for a Forest card" (basic
    # land type rather than the literal word "land"). Original ramp
    # pattern required ``\bland\b``. Pinned in commit 085c256.
    "Three Visits": {
        "oracle_text": (
            "Search your library for a Forest card, put it onto the "
            "battlefield, then shuffle."
        ),
        "type_line": "Sorcery",
    },

    # Toxic Deluge — "All creatures get -X/-X" mass-shrink wipe; no
    # existing pattern matched the shape. Pinned in commit 085c256.
    "Toxic Deluge": {
        "oracle_text": (
            "As an additional cost to cast this spell, pay X life.\n"
            "All creatures get -X/-X until end of turn."
        ),
        "type_line": "Sorcery",
    },

    # Victimize — reanimation whose return clause ("return the chosen
    # cards to the battlefield tapped") names the creature cards a
    # SENTENCE EARLIER — deliberately out of reach of the clause-bound
    # oracle patterns; carried by combo_detection's hard-tagged
    # _REANIMATION_SPELLS set instead. This fixture pins that gap.
    "Victimize": {
        "oracle_text": (
            "Choose two target creature cards in your graveyard. "
            "Sacrifice a creature. If you do, return the chosen cards "
            "to the battlefield tapped."
        ),
        "type_line": "Sorcery",
    },

    # Widespread Brutality — "deals damage equal to its power to each
    # non-Army creature": the equal-to sweep form (no digits, no
    # literal X) the round-2 wipe pattern must catch.
    "Widespread Brutality": {
        "oracle_text": (
            "Amass Zombies 2, then the Army you amassed deals damage "
            "equal to its power to each non-Army creature. (To amass "
            "Zombies 2, put two +1/+1 counters on an Army you "
            "control. It's also a Zombie. If you don't control an "
            "Army, create a 0/0 black Zombie Army creature token "
            "first.)"
        ),
        "type_line": "Sorcery",
    },

    # Worldgorger Dragon — the reanimation-combo poster child (with
    # Animate Dead). Printed MV 6; the round-2 combo-speed fix must
    # price it at Animate Dead's MV instead of 6 so the pair reads
    # early-game (B4 floor), not "8 total mana = late-game B3".
    "Worldgorger Dragon": {
        "oracle_text": (
            "Flying, trample\n"
            "When this creature enters, exile all other permanents "
            "you control.\n"
            "When this creature leaves the battlefield, return the "
            "exiled cards to the battlefield under their owners' "
            "control."
        ),
        "type_line": "Creature — Nightmare Dragon",
    },

    # Wrath of God — baseline destroy-all template. Used as a
    # control value to confirm the standard pattern still works
    # after the multi-paragraph parser tweaks.
    "Wrath of God": {
        "oracle_text": "Destroy all creatures. They can't be regenerated.",
        "type_line": "Sorcery",
    },

    # Wrenn's Resolve — the bare two-card impulse-draw template
    # (functional Reckless Impulse reprint), no keyword riders.
    "Wrenn's Resolve": {
        "oracle_text": (
            "Exile the top two cards of your library. Until the end "
            "of your next turn, you may play those cards."
        ),
        "type_line": "Sorcery",
    },
}


def oracle(name: str) -> dict[str, str]:
    """Convenience accessor: returns ``{"oracle_text", "type_line"}``
    for ``name``. Raises ``KeyError`` with a helpful message when the
    card hasn't been added to the fixture yet.
    """
    if name not in ORACLES:
        raise KeyError(
            f"No real-oracle fixture for {name!r}. Add it to "
            f"tests/fixtures/real_oracles.py — do NOT synthesize "
            f"oracle text in tests; copy verbatim from Scryfall."
        )
    return ORACLES[name]
