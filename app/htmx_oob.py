"""Helpers for building HTMX out-of-band swap wrappers safely.

The blueprints used to build these as f-strings inline, which made static
analyzers (and reviewers) wary because user input could in theory flow into
the constructed HTML. These helpers force the variable parts (target IDs,
swap modes, target selectors) through validation so an attacker-controlled
value cannot inject attributes or break out of the wrapper element.

Returns plain ``str`` (not Markup) because callers concatenate the wrapper
with other raw HTML and pass the result to ``make_response`` / WebSocket
``send`` — a Markup return would escape the surrounding strings on concat.
"""

from markupsafe import escape

_ALLOWED_SWAP_MODES = {
    "innerHTML",
    "outerHTML",
    "beforebegin",
    "afterbegin",
    "beforeend",
    "afterend",
    "delete",
    "true",
    "morph",
}


def _check_swap(swap):
    if swap not in _ALLOWED_SWAP_MODES:
        raise ValueError(f"Unsupported hx-swap-oob mode: {swap!r}")
    return swap


def oob_by_id(target_id, swap, inner_html="", *, tag="div", css_class=None):
    """Wrap inner_html in <tag id="..." hx-swap-oob="swap">...</tag>.

    target_id and css_class are escaped, so callers can pass integers,
    model PKs, or composed strings safely. inner_html must already be a
    pre-rendered, auto-escaped Jinja fragment.
    tag defaults to 'div'; pass 'span', 'li', etc. for inline cases.
    """
    _check_swap(swap)
    safe_id = str(escape(str(target_id)))
    safe_tag = str(escape(str(tag)))
    class_attr = f' class="{escape(css_class)}"' if css_class else ""
    return (
        f'<{safe_tag} id="{safe_id}" hx-swap-oob="{swap}"{class_attr}>'
        f"{inner_html}</{safe_tag}>"
    )


def oob_to_selector(swap, target_selector, inner_html=""):
    """Wrap inner_html in <div hx-swap-oob="swap:selector">...</div>.

    target_selector is escaped. Use this for swap modes that embed a CSS
    selector (most commonly beforeend:#some-list-id).
    """
    _check_swap(swap)
    safe_sel = str(escape(str(target_selector)))
    return f'<div hx-swap-oob="{swap}:{safe_sel}">{inner_html}</div>'


def message_list_append_oob(conv_id_str, inner_html):
    """Append inner_html to the open conversation's #message-list — but only
    if that conversation is ``conv_id_str``.

    The wrapper carries the conversation id, and the client drops the append
    (htmx:oobBeforeSwap in chat.js) when the pane on screen is a different
    conversation: a socket stays subscribed to the previous conversation until
    its next subscribe frame lands, and a reconnect backfill can come back
    after the user moved on, so an unchecked append could drop one
    conversation's message into another's pane.
    """
    safe_conv = str(escape(str(conv_id_str)))
    return (
        f'<div hx-swap-oob="beforeend:#message-list" '
        f'data-conversation-id="{safe_conv}">{inner_html}</div>'
    )


def oob_self(element_id, html):
    """Mark the root of ``html`` (the element with ``element_id``) as an OOB
    swap of itself.

    For partials whose root already carries the id they replace, such as the
    composer. Wrapping them with ``oob_by_id`` instead left a second element
    with the same id nested inside the first after every swap.
    """
    marker = f'id="{escape(str(element_id))}"'
    if marker not in html:
        raise ValueError(f"No element with id {element_id!r} in the fragment")
    return html.replace(marker, f'{marker} hx-swap-oob="true"', 1)
