import logging

from django.utils import timezone
from telegram import (
    Update,
    Message as TGMessage,
    User as TGUser,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import CallbackContext, Filters

from bot import redis
from core.models import Button, Chat, Message, Reaction
from .filters import reaction_filter
from .markup import make_reply_markup_from_chat
from .utils import (
    message_handler,
    try_delete,
    get_message_type,
    get_chat_from_tg_chat,
    get_forward_from,
    get_user,
    get_forward_from_chat,
)

logger = logging.getLogger(__name__)


def process_message(update: Update, context: CallbackContext, msg_type: str, chat: Chat):
    msg: TGMessage = update.effective_message
    bot = context.bot

    chat, reply_markup = make_reply_markup_from_chat(update, context, chat=chat)

    config = {
        'chat_id': msg.chat_id,
        'text': msg.text_html,
        'caption': msg.caption_html,
        'disable_notification': True,
        'parse_mode': 'HTML',
        'reply_markup': reply_markup,
        # files
        'photo': msg.photo and msg.photo[0].file_id,
        'video': msg.video and msg.video.file_id,
        'animation': msg.animation and msg.animation.file_id,
    }
    if msg_type == 'photo':
        sent_msg = bot.send_photo(**config)
    elif msg_type == 'video':
        sent_msg = bot.send_video(**config)
    elif msg_type == 'animation':
        sent_msg = bot.send_animation(**config)
    elif msg_type == 'text':
        sent_msg = bot.send_message(**config)
    elif msg_type == 'album':
        config.pop('chat_id')
        config['text'] = '^'
        sent_msg = msg.reply_text(**config)
    else:
        sent_msg = None

    if sent_msg:
        if msg_type != 'album':
            try_delete(bot, update, msg)
        Message.objects.create_from_tg_ids(
            sent_msg.chat_id,
            sent_msg.message_id,
            date=timezone.make_aware(msg.date),
            original_message_id=msg.message_id,
            from_user=get_user(update),
            forward_from=get_forward_from(msg),
            forward_from_chat=get_forward_from_chat(msg),
            forward_from_message_id=msg.forward_from_message_id,
        )


def check_force_skip(msg: TGMessage):
    """
    Returns:
        True if should message should be skipped.
    """
    text: str = msg.text or msg.caption
    if text and text.startswith('--'):
        return True


def check_force_repost(msg: TGMessage):
    """
    Check if reposting should be forced. If so - patch message and remove "++" (force mark).

    Returns:
        None - skip reposting, bool - forced/unforced reposting.
    """
    text: str = msg.text or msg.caption
    force = bool(text and text.startswith('++'))
    if force:
        if msg.text and len(msg.text) > 2:
            msg.text = msg.text[2:]
        elif msg.text:
            # can't repost message without text
            return
        if msg.caption and len(msg.caption) > 2:
            msg.caption = msg.caption[2:]
        else:
            msg.caption = None
    return force


def check_force(msg: TGMessage):
    if check_force_skip(msg):
        return
    return check_force_repost(msg)


@message_handler(
    Filters.group & ~Filters.reply & ~Filters.status_update.left_chat_member &
    (Filters.photo | Filters.video | Filters.animation | Filters.forwarded | Filters.text)
)
def handle_message(update: Update, context: CallbackContext):
    msg: TGMessage = update.effective_message

    force = check_force(msg)
    if force is None:
        return

    chat = get_chat_from_tg_chat(update.effective_chat)
    allowed_types = chat.allowed_types
    allow_forward = 'forward' in allowed_types

    msg_type = get_message_type(msg)
    forward = bool(msg.forward_date)

    if force or msg_type in allowed_types or forward and allow_forward:
        process_message(update, context, msg_type, chat)


@message_handler(Filters.private & Filters.text & reaction_filter)
def handle_reaction_response(update: Update, context: CallbackContext):
    user: TGUser = update.effective_user
    msg = update.effective_message
    reaction = msg.text

    # todo: validate reaction

    some_message_id = redis.awaited_reaction(user.id)
    try:
        message = Message.objects.prefetch_related().get(id=some_message_id)
    except Message.DoesNotExist:
        logger.debug(f"Message {some_message_id} doesn't exist.")
        return

    mids = message.ids
    Reaction.objects.react(
        user=user,
        button_text=reaction,
        **mids,
    )
    reactions = Button.objects.reactions(**mids)
    _, reply_markup = make_reply_markup_from_chat(update, context, reactions, message=message)
    context.bot.edit_message_reply_markup(reply_markup=reply_markup, **mids)
    msg.reply_text(f"Reacted with {reaction}")
    redis.stop_awaiting_reaction(user.id)


@message_handler(
    Filters.private &
    (Filters.photo | Filters.video | Filters.animation | Filters.forwarded | Filters.text)
)
def handle_create(update: Update, context: CallbackContext):
    user: TGUser = update.effective_user
    msg: TGMessage = update.effective_message
    # todo: ask user for buttons and other settings
    redis.save_creation(user.id, msg.to_dict(), ['👍', '👎'])
    msg.reply_text(
        "Press 'publish' and choose your channel.\n"
        "Publishing will be available for 1 hour.",
        reply_markup=InlineKeyboardMarkup.from_button(
            InlineKeyboardButton(
                "publish",
                switch_inline_query="publish",
            )
        )
    )
