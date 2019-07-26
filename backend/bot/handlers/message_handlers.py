import logging
from collections import OrderedDict

from django.utils import timezone
from emoji import UNICODE_EMOJI
from telegram import (
    Update,
    Message as TGMessage,
    User as TGUser,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    Bot,
)
from telegram.ext import CallbackContext, Filters

from bot import redis
from bot.redis import State
from core.models import Button, Chat, Message, Reaction, MessageToPublish, UserButtons
from .filters import StateFilter
from .magic_marks import process_magic_mark
from .markup import make_reply_markup_from_chat, make_reactions_keyboard
from .utils import (
    message_handler,
    try_delete,
    get_message_type,
    get_chat_from_tg_chat,
    get_forward_from,
    get_user,
    get_forward_from_chat,
    clear_buttons,
)

logger = logging.getLogger(__name__)


def repost_message(msg: TGMessage, bot: Bot, msg_type, reply_markup):
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
        'document': msg.document and msg.document.file_id,
        'audio': msg.audio and msg.audio.file_id,
        'voice': msg.voice and msg.voice.file_id,
        'video_note': msg.video_note and msg.video_note.file_id,
        'sticker': msg.sticker and msg.sticker.file_id,
    }
    sender_map = {
        'text': bot.send_message,
        'link': bot.send_message,
        'photo': bot.send_photo,
        'video': bot.send_video,
        'animation': bot.send_animation,
        'document': bot.send_document,
        'audio': bot.send_audio,
        'voice': bot.send_voice,
        'video_note': bot.send_video_note,
        'sticker': bot.send_sticker,
    }
    if msg_type in sender_map:
        sent_msg = sender_map[msg_type](**config)
    else:
        sent_msg = None
    return sent_msg


def process_message(
    update: Update,
    context: CallbackContext,
    msg_type: str,
    chat: Chat,
    anonymous: bool,
    buttons=None,
    repost=False,
):
    msg: TGMessage = update.effective_message
    bot = context.bot

    chat, reply_markup = make_reply_markup_from_chat(
        update,
        context,
        buttons,
        chat=chat,
        anonymous=anonymous,
    )

    should_repost = (chat.repost or repost) and msg_type != 'album'

    if should_repost:
        sent_msg = repost_message(msg, bot, msg_type, reply_markup)
    else:
        sent_msg = msg.reply_text(
            text='^',
            disable_notification=True,
            reply_markup=reply_markup,
        )

    logger.debug(f"sent_msg: {sent_msg}")
    if sent_msg:
        if should_repost:
            try_delete(bot, update, msg)
        Message.objects.create_from_tg_ids(
            sent_msg.chat_id,
            sent_msg.message_id,
            buttons=buttons,
            anonymous=anonymous,
            date=timezone.make_aware(msg.date),
            original_message_id=msg.message_id,
            from_user=get_user(update),
            forward_from=get_forward_from(msg),
            forward_from_chat=get_forward_from_chat(msg),
            forward_from_message_id=msg.forward_from_message_id,
        )


@message_handler(Filters.group & ~Filters.reply & ~Filters.status_update)
def handle_message(update: Update, context: CallbackContext):
    msg: TGMessage = update.effective_message

    force, anonymous, skip, buttons = process_magic_mark(msg)
    logger.debug(f"force: {force}, anonymous: {anonymous}, skip: {skip}, buttons: {buttons}")
    if skip:
        logger.debug('skipping message processing')
        return

    chat = get_chat_from_tg_chat(update.effective_chat)
    allowed_types = chat.allowed_types
    allow_forward = 'forward' in allowed_types

    msg_type = get_message_type(msg)
    forward = bool(msg.forward_date)
    logger.debug(f"msg_type: {msg_type}, forward: {forward}")

    if force > 0 or msg_type in allowed_types or forward and allow_forward:
        process_message(
            update,
            context,
            msg_type,
            chat,
            anonymous,
            buttons,
            repost=force > 1,
        )


@message_handler(Filters.private & (Filters.text | Filters.sticker) & StateFilter.reaction)
def handle_reaction_response(update: Update, context: CallbackContext):
    user: TGUser = update.effective_user
    msg = update.effective_message
    reaction = msg.text or (msg.sticker and msg.sticker.emoji)

    if reaction not in UNICODE_EMOJI:
        msg.reply_text(f"Reaction should be a single emoji.")
        return

    some_message_id = redis.get_key(user.id, 'message_id')
    try:
        message = Message.objects.prefetch_related().get(id=some_message_id)
    except Message.DoesNotExist:
        logger.debug(f"Message {some_message_id} doesn't exist.")
        msg.reply_text(f"Received invalid message ID from /start command.")
        return

    mids = message.ids
    _, button = Reaction.objects.react(
        user=user,
        button_text=reaction,
        **mids,
    )
    if not button:
        msg.reply_text(f"Post already has too many reactions.")
        return
    reactions = Button.objects.reactions(**mids)
    _, reply_markup = make_reply_markup_from_chat(update, context, reactions, message=message)
    context.bot.edit_message_reply_markup(reply_markup=reply_markup, **mids)
    msg.reply_text(f"Reacted with {reaction}")


@message_handler(Filters.private & StateFilter.create_start)
def handle_create_start(update: Update, context: CallbackContext):
    user: TGUser = update.effective_user
    msg: TGMessage = update.effective_message
    MessageToPublish.objects.create(user_id=user.id, message=msg.to_dict())

    buttons = [
        *UserButtons.buttons_list(user.id),
        '👍 👎',
        '✅ ❌',
    ]
    buttons = list(OrderedDict.fromkeys(buttons))  # remove duplicated buttons
    buttons = buttons[:3]
    msg.reply_text(
        "Now specify buttons.",
        reply_markup=ReplyKeyboardMarkup.from_column(
            [
                *buttons,
                'none',
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
    )
    redis.set_state(user, State.create_buttons)


@message_handler(Filters.private & Filters.text & StateFilter.create_buttons)
def handle_create_buttons(update: Update, context: CallbackContext):
    user: TGUser = update.effective_user
    msg: TGMessage = update.effective_message

    if msg.text == 'none':
        buttons = []
    else:
        buttons = clear_buttons(msg.text.split(), emojis=True)
        if not buttons:
            msg.reply_text("Buttons should be emojis.")
            return
        UserButtons.create(user.id, buttons)

    mtp = MessageToPublish.last(user.id)
    mtp.buttons = buttons
    mtp.save()

    msg.reply_text("Press 'publish' and choose chat/channel.")
    message = mtp.message_tg
    msg_type = get_message_type(message)
    reply_markup = make_reactions_keyboard(buttons, blank=True)
    reply_markup.inline_keyboard.append([
        InlineKeyboardButton(
            "publish",
            switch_inline_query=str(mtp.id),
        )
    ])
    repost_message(message, context.bot, msg_type, reply_markup)
    redis.set_state(user, State.create_end)
