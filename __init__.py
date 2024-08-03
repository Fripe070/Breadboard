import dataclasses
import json
import sqlite3
from typing import TYPE_CHECKING, Self, TypedDict, cast

import discord
from discord import app_commands

import breadcord
from breadcord.module import ModuleCog

if TYPE_CHECKING:
    from pathlib import Path

GenericID = int
ChannelID = GenericID
GuildID = GenericID
MessageID = GenericID
AnyEmoji = discord.Emoji | discord.PartialEmoji | str


class ChannelConfigOverrideDict(TypedDict):
    override_for: ChannelID
    required_reactions: int | None
    extra_emojis: list[str] | None


class ChannelConfigDict(TypedDict):
    channel_id: ChannelID
    required_reactions: int
    extra_emojis: list[str]

    channel_overrides: list[ChannelConfigOverrideDict]


@dataclasses.dataclass
class ChannelConfigOverride:
    override_for: ChannelID
    required_reactions: int | None = None
    extra_emojis: list[discord.PartialEmoji] | None = None


@dataclasses.dataclass
class StarboardChannelConfig:
    channel_id: ChannelID
    required_reactions: int
    watched_emojis: list[discord.PartialEmoji]

    channel_overrides: dict[ChannelID, ChannelConfigOverride] = dataclasses.field(default_factory=dict)

    def is_watched(self, emoji: AnyEmoji) -> bool:
        if isinstance(emoji, str):
            emoji = discord.PartialEmoji.from_str(emoji)
        return any(
            watched_emoji == emoji
            for watched_emoji in self.watched_emojis
        )


class GuildConfigs(dict[GuildID, dict[ChannelID, StarboardChannelConfig]]):
    @classmethod
    def load(cls, config_json: dict[str, list[ChannelConfigDict]]) -> Self:
        return cls({
            int(guild_id): {  # JSON does not support int keys, which is why we cast
                channel["channel_id"]: StarboardChannelConfig(
                    channel_id=channel["channel_id"],
                    required_reactions=channel["required_reactions"],
                    watched_emojis=list(map(discord.PartialEmoji.from_str, channel["extra_emojis"])),
                    channel_overrides={
                        override["override_for"]: ChannelConfigOverride(
                            override_for=override["override_for"],
                            required_reactions=override["required_reactions"],
                            extra_emojis=list(
                                map(discord.PartialEmoji.from_str, override["extra_emojis"])
                            ) if override["extra_emojis"] else None,
                        )
                        for override in channel.get("channel_overrides", [])
                    }
                )
                for channel in channels
            }
            for guild_id, channels in config_json.items()
        })

    def dump(self) -> dict[str, list[ChannelConfigDict]]:
        return {
            str(guild_id): [
                {
                    "channel_id": channel.channel_id,
                    "required_reactions": channel.required_reactions,
                    "extra_emojis": list(map(str, channel.watched_emojis)),
                    "channel_overrides": [
                        {
                            "override_for": override.override_for,
                            "required_reactions": override.required_reactions,
                            "extra_emojis": list(map(str, override.extra_emojis)) if override.extra_emojis else None,
                        }
                        for override in channel.channel_overrides.values()
                    ],
                }
                for channel in channels.values()
            ]
            for guild_id, channels in self.items()
        }


class OriginalMessageButton(discord.ui.View):
    def __init__(
        self,
        *,
        original_message_url: str,
        star_count: int,
        star_emoji: discord.PartialEmoji | discord.Emoji | str = "⭐",
    ) -> None:
        super().__init__()
        self.add_item(
            discord.ui.Button(
                label=f"{star_count} | Original Message",
                url=original_message_url,
                style=discord.ButtonStyle.link,
                emoji=star_emoji,
            ),
        )


def get_top_emoji(reactions_map: dict[AnyEmoji, list[discord.User | discord.Member]]) -> AnyEmoji:
    most_popular_emoji: tuple[AnyEmoji, int] = (next(iter(reactions_map.keys())), 0)
    for emoji, users in reactions_map.items():
        if len(users) > most_popular_emoji[1]:
            most_popular_emoji = (emoji, len(users))
    return most_popular_emoji[0]


class ManageStarboardButtons(discord.ui.View):
    def __init__(self, *, starboard_channel_config: StarboardChannelConfig) -> None:
        super().__init__()
        self.starboard_channel_config = starboard_channel_config

    @staticmethod
    async def request_emoji(interaction: discord.Interaction, *, to_add: bool) -> discord.PartialEmoji | None:
        modal = EmojiAddRemoveModal(to_add=to_add)
        await interaction.response.send_modal(modal)
        await modal.wait()
        return modal.emoji

    @discord.ui.button(label="Add Emoji", style=discord.ButtonStyle.green)
    async def add_emoji(self, interaction: discord.Interaction, _) -> None:
        emoji = await self.request_emoji(interaction, to_add=True)
        if emoji is None:
            return
        self.starboard_channel_config.watched_emojis.append(emoji)
        await interaction.followup.send(
            f"Emoji `{discord.utils.escape_markdown(str(emoji))}` added to watched emojis",
            ephemeral=True,
        )

    @discord.ui.button(label="Remove Emoji", style=discord.ButtonStyle.red)
    async def remove_emoji(self, interaction: discord.Interaction, _) -> None:
        emoji = await self.request_emoji(interaction, to_add=False)
        if emoji is None:
            return
        followup = cast(discord.Webhook, interaction.followup)
        if emoji not in self.starboard_channel_config.watched_emojis:
            return await followup.send(
                f"Emoji `{discord.utils.escape_markdown(str(emoji))}` is not being watched",
                ephemeral=True,
            )
        self.starboard_channel_config.watched_emojis.remove(emoji)
        await followup.send(
            f"Emoji `{discord.utils.escape_markdown(str(emoji))}` removed from watched emojis",
            ephemeral=True,
        )

    @discord.ui.button(label="Override Config", style=discord.ButtonStyle.blurple)
    async def override_config(self, interaction: discord.Interaction, _) -> None:
        modal = OverrideModal()
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.override is None:
            return
        self.starboard_channel_config.channel_overrides[modal.override.override_for] = modal.override
        await interaction.followup.send(
            f"Config overridden for channel {modal.override.override_for}",
            ephemeral=True,
        )


class EmojiAddRemoveModal(discord.ui.Modal):
    def __init__(self, to_add: bool) -> None:
        super().__init__(
            title=("Add" if to_add else "Remove") + " Emoji",
            timeout=None,
        )
        self.to_add = to_add
        self.emoji: None | discord.PartialEmoji = None

        self.emoji_input = discord.ui.TextInput(
            label=f"Emoji to {'add to' if to_add else 'remove from'} watched emojis",
            placeholder="Enter a unicode or custom (<:name:id>) emoji",
            min_length=1,
        )
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.emoji = discord.PartialEmoji.from_str(self.emoji_input.value)
        self.stop()
        await interaction.response.defer()


class OverrideModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(
            title="Override Config For Channel",
            timeout=None,
        )
        self.override: None | ChannelConfigOverride = None

    channel_input = discord.ui.TextInput(
        label="Channel to override config for",
        placeholder="Enter the channel ID",
        min_length=1,
        required=True,
    )
    required_reactions_input = discord.ui.TextInput(
        label="Required reactions",
        placeholder="Enter the required reactions count",
        required=False,
    )
    extra_emojis_input = discord.ui.TextInput(
        label="Extra emojis as a comma separated list",
        placeholder="👍, 👎",
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            channel_input = int(self.channel_input.value)
        except ValueError:
            return await interaction.response.send_message("Invalid channel ID", ephemeral=True)
        required_reactions: int | None = None
        if self.required_reactions_input.value:
            try:
                required_reactions = int(self.required_reactions_input.value)
            except ValueError:
                return await interaction.response.send_message("Invalid required reactions count", ephemeral=True)
            if required_reactions <= 0:
                return await interaction.response.send_message(
                    "Required reactions must be greater than 0",
                    ephemeral=True,
                )

        self.override = ChannelConfigOverride(
            override_for=channel_input,
            required_reactions=required_reactions,
            extra_emojis=[
                discord.PartialEmoji.from_str(emoji.strip())
                for emoji in self.extra_emojis_input.value.split(",")
            ] if self.extra_emojis_input.value else None,
        )
        self.stop()
        await interaction.response.defer()


class Breadboard(ModuleCog):
    command_group = app_commands.Group(
        name="starboard",
        description="Manage starboards",
        default_permissions=None,
        guild_only=True,
    )

    @command_group.command(name="add")
    async def starboard_add_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        required_reactions: int | None = None,
    ) -> None:
        if required_reactions is None:
            required_reactions = cast(int, self.settings.default_required_stars.value)
        if required_reactions <= 0:
            return await interaction.response.send_message("Required reactions must be greater than 0", ephemeral=True)
        if channel.id in self.guild_configs.get(interaction.guild_id, {}):
            return await interaction.response.send_message(
                f"Channel is already a starboard. "
                f"Use `/starboard modify` to change settings, or `/starboard remove` to remove it as a starboard.",
                ephemeral=True
            )

        config = StarboardChannelConfig(
            channel_id=channel.id,
            required_reactions=required_reactions,
            watched_emojis=[
                discord.PartialEmoji.from_str(emoji)
                for emoji in cast(list[str], self.settings.default_emojis.value)
            ],
        )
        try:
            await self.fetch_starboard_webhook(channel_config=config)
        except discord.Forbidden:
            return await interaction.response.send_message(
                f"The bot doesn't have access to the starboard channel: {channel.mention}",
                ephemeral=True,
            )

        self.guild_configs.setdefault(interaction.guild_id, {})[channel.id] = config
        await interaction.response.send_message(
            f"Starboard channel added: {channel.mention} with {required_reactions} required reactions",
            view=ManageStarboardButtons(starboard_channel_config=self.guild_configs[interaction.guild_id][channel.id]),
            ephemeral=True,
        )

    @command_group.command(name="modify")
    async def starboard_modify_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        required_reactions: int | None = None,
    ) -> None:
        if required_reactions is None:
            required_reactions = cast(int, self.settings.default_required_stars.value)
        if required_reactions <= 0:
            return await interaction.response.send_message("Required reactions must be greater than 0", ephemeral=True)
        if channel.id not in self.guild_configs.get(interaction.guild_id, {}):
            return await interaction.response.send_message(f"Channel is not a starboard.", ephemeral=True)

        relevant_config: StarboardChannelConfig = self.guild_configs[interaction.guild_id][channel.id]
        if required_reactions is not None:
            relevant_config.required_reactions = required_reactions
            message = f"Modifying starboard channel {channel.mention} to require {required_reactions} reactions"
        else:
            message = f"Modifying starboard channel {channel.mention}"
        await interaction.response.send_message(
            message,
            view=ManageStarboardButtons(starboard_channel_config=relevant_config),
            ephemeral=True,
        )

    @command_group.command(name="remove")
    async def starboard_remove_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if channel.id not in self.guild_configs.get(interaction.guild_id, {}):
            return await interaction.response.send_message(f"Channel is not a starboard.", ephemeral=True)

        del self.guild_configs[interaction.guild_id][channel.id]
        if not self.guild_configs[interaction.guild_id]:
            del self.guild_configs[interaction.guild_id]
        await interaction.response.send_message(f"Starboard channel removed: {channel.mention}", ephemeral=True)

    def __init__(self, module_id: str) -> None:
        super().__init__(module_id)
        self.connection = sqlite3.connect(self.module.storage_path / "starred_messages.db")
        self.setup_db(self.connection)

        self._guild_configs_path: Path = self.module.storage_path / "guild_configs.json"
        self.guild_configs: GuildConfigs
        if self._guild_configs_path.exists():
            with self._guild_configs_path.open("r", encoding="utf-8") as f:
                self.guild_configs = GuildConfigs.load(json.load(f))
        else:
            self.guild_configs = GuildConfigs()
            with self._guild_configs_path.open("w", encoding="utf-8") as f:
                json.dump({}, f)

    async def cog_load(self) -> None:
        failed: bool = False
        for guild_configs in self.guild_configs.values():
            for channel_config in guild_configs.values():
                if channel_config.required_reactions <= 0:
                    self.logger.error(
                        f"Starboard channel {channel_config.channel_id} has a required reactions count of 0 or less",
                    )
                    failed = True
        if failed:
            raise RuntimeError("Issues with configuration, see logs for details.")

    async def cog_unload(self) -> None:
        self.connection.close()
        with self._guild_configs_path.open("w", encoding="utf-8") as f:
            json.dump(self.guild_configs.dump(), f, indent=4)  # TODO: No indent in production

    def setup_db(self, connection: sqlite3.Connection) -> None:
        # TODO: auto migrate v1 -> v2
        connection.execute(
            "CREATE TABLE IF NOT EXISTS starred_messages ("
            "   original_id INTEGER PRIMARY KEY NOT NULL UNIQUE,"
            "   starboard_message_id INTEGER NOT NULL UNIQUE,"
            "   starboard_channel_id INTEGER NOT NULL,"
            "   star_count INTEGER NOT NULL"
            ")",
        )
        connection.commit()

    async def fetch_message_by_id(self, channel_id: int, message_id: int) -> discord.Message:
        partial_channel = self.bot.get_partial_messageable(channel_id)
        return await partial_channel.fetch_message(message_id)

    async def fetch_starboard_webhook(self, *, channel_config: StarboardChannelConfig) -> discord.Webhook:
        channel = (
            self.bot.get_channel(channel_config.channel_id)
            or await self.bot.fetch_channel(channel_config.channel_id)
        )

        if not isinstance(channel, discord.TextChannel):
            raise ValueError(f"Starboard channel {channel} is not a text channel")

        webhook_name: str = self.settings.webhook_name.value  # pyright: ignore [reportAssignmentType]
        try:
            webhook = discord.utils.find(
                lambda w: w.name == webhook_name,
                await channel.webhooks(),
            )
            if not webhook:
                webhook = await channel.create_webhook(name=webhook_name)
            return webhook
        except discord.Forbidden as error:
            raise RuntimeError(
                f"Bot doesn't have the \"Manage Webhooks\" permission in starboard channel: #{channel}",
            ) from error

    def delete_from_db(self, *, message_id: int) -> None:
        self.connection.execute(
            "DELETE FROM starred_messages WHERE original_id = ?",
            (message_id,),
        )
        self.connection.commit()

    @ModuleCog.listener(name="on_raw_reaction_add")
    @ModuleCog.listener(name="on_raw_reaction_remove")
    @ModuleCog.listener(name="on_raw_reaction_clear")
    @ModuleCog.listener(name="on_reaction_clear_emoji")
    async def on_raw_reaction_update(self, reaction_event: discord.RawReactionActionEvent) -> None:
        if reaction_event.guild_id is None or reaction_event.guild_id not in self.guild_configs:
            return
        # We don't want to be able to star messages sent in a starboard channel
        if reaction_event.channel_id in self.guild_configs[reaction_event.guild_id]:
            return
        # A reaction will only ever change things for the config that is watching it
        relevant_configs: list[StarboardChannelConfig] = [
            config
            for config in self.guild_configs[reaction_event.guild_id].values()
            if config.is_watched(reaction_event.emoji)
        ]
        if not relevant_configs:
            return

        try:
            starred_message: discord.Message = await self.fetch_message_by_id(
                channel_id=reaction_event.channel_id,
                message_id=reaction_event.message_id,
            )
            referencing: discord.Message | None = None
            if starred_message.reference and isinstance(starred_message.reference.resolved, discord.Message):
                referencing = starred_message.reference.resolved
        except discord.errors.NotFound:
            return

        reaction_map: dict[AnyEmoji, list[discord.User | discord.Member]] = {
            reaction.emoji: [user async for user in reaction.users()]
            for reaction in starred_message.reactions
        }

        for channel_config in relevant_configs:
            await self.update_starboard_message(
                message=starred_message,
                referencing=referencing,
                reaction_map=reaction_map,
                channel_config=channel_config,
            )

    async def update_starboard_message(
        self,
        message: discord.Message,
        referencing: discord.Message | None,
        reaction_map: dict[AnyEmoji, list[discord.User | discord.Member]],
        channel_config: StarboardChannelConfig,
    ) -> None:
        relevant_reaction_map: dict[AnyEmoji, list[discord.User | discord.Member]] = {
            emoji: users
            for emoji, users in reaction_map.items()
            if channel_config.is_watched(emoji)
        }
        unique_reaction_count: int = len({user for users in relevant_reaction_map.values() for user in users})

        sql_response: tuple[MessageID, int] | None = self.connection.execute(
            "SELECT starboard_message_id, star_count FROM starred_messages WHERE original_id = ?",
            (message.id,),
        ).fetchone()

        if unique_reaction_count >= channel_config.required_reactions:
            if sql_response is None:  # Newly starred message
                await self.create_starboard_message(
                    message=message,
                    referencing=referencing,
                    relevant_reaction_map=relevant_reaction_map,
                    config=channel_config,
                )
            elif unique_reaction_count != sql_response[1]:  # Star count changed
                await self.update_starboard_message_button(
                    message=message,
                    relevant_reaction_map=relevant_reaction_map,
                    starboard_message_id=sql_response[0],
                    config=channel_config,
                )
        elif sql_response is not None:  # An already stared message doesn't have enough reactions
            await self.delete_starboard_message(
                message=message,
                config=channel_config,
                starboard_message_id=sql_response[0],
            )
        else:
            pass  # Plink

    async def create_starboard_message(
        self,
        message: discord.Message,
        referencing: discord.Message | None,
        relevant_reaction_map: dict[AnyEmoji, list[discord.User | discord.Member]],
        config: StarboardChannelConfig,
    ) -> None:
        webhook = await self.fetch_starboard_webhook(channel_config=config)

        embeds: list[discord.Embed] = [embed for embed in message.embeds if embed.type == "rich"]
        if referencing:
            attachment_url: str | None = (
                referencing.attachments[0].url
                if referencing.attachments
                else (
                    referencing.embeds[0].thumbnail.url
                    if referencing.embeds and referencing.embeds[0].thumbnail
                    else None
                )
            )
            embeds.insert(0, (
                discord.Embed(
                    description=referencing.content,
                    url=referencing.jump_url,
                ).set_author(
                    name=referencing.author.display_name,
                    icon_url=referencing.author.avatar.url if referencing.author.avatar else None,
                ).set_image(
                    url=attachment_url,
                )
            ))
        unique_reaction_count: int = len({user for users in relevant_reaction_map.values() for user in users})

        webhook_msg = await webhook.send(
            username=message.author.display_name,
            avatar_url=avatar.url if (avatar := message.author.avatar) else None,
            content=message.content,
            embeds=embeds[:10],
            files=[await attachment.to_file() for attachment in message.attachments],
            allowed_mentions=discord.AllowedMentions.none(),
            view=OriginalMessageButton(
                original_message_url=message.jump_url,
                star_count=unique_reaction_count,
                star_emoji=get_top_emoji(relevant_reaction_map),
            ),
            wait=True,
        )

        self.connection.execute(
            "INSERT INTO starred_messages (original_id, starboard_message_id, star_count) VALUES (?, ?, ?)",
            (message.id, webhook_msg.id, unique_reaction_count),
        )
        self.connection.commit()

    async def update_starboard_message_button(
        self,
        message: discord.Message,
        relevant_reaction_map: dict[AnyEmoji, list[discord.User | discord.Member]],
        config: StarboardChannelConfig,
        starboard_message_id: int,
    ) -> None:
        unique_reaction_count: int = len({user for users in relevant_reaction_map.values() for user in users})

        self.connection.execute(
            "UPDATE starred_messages SET star_count = ? WHERE original_id = ?",
            (unique_reaction_count, message.id),
        )
        self.connection.commit()

        try:
            webhook = await self.fetch_starboard_webhook(channel_config=config)
            await webhook.edit_message(
                message_id=starboard_message_id,
                view=OriginalMessageButton(
                    original_message_url=message.jump_url,
                    star_count=unique_reaction_count,
                    star_emoji=get_top_emoji(relevant_reaction_map),
                ),
            )
        except discord.NotFound:
            self.delete_from_db(message_id=message.id)
            raise

    async def delete_starboard_message(
        self,
        message: discord.Message,
        config: StarboardChannelConfig,
        starboard_message_id: int,
    ) -> None:
        self.delete_from_db(message_id=message.id)
        webhook = await self.fetch_starboard_webhook(channel_config=config)
        await webhook.delete_message(starboard_message_id)


async def setup(bot: breadcord.Bot, module: breadcord.module.Module) -> None:
    await bot.add_cog(Breadboard(module.id))
