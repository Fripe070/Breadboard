import dataclasses
import json
import sqlite3
from typing import TYPE_CHECKING, Self, TypedDict, cast

import breadcord
import discord
from breadcord.module import ModuleCog
from discord import app_commands

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
    allow_self_star: bool | None
    allow_any_emoji: bool | None


@dataclasses.dataclass
class ChannelConfigOverride:
    override_for: ChannelID
    required_reactions: int | None = None
    extra_emojis: list[discord.PartialEmoji] | None = None
    allow_self_star: bool | None = None
    allow_any_emoji: bool | None = None


class ChannelConfigDict(TypedDict):
    channel_id: ChannelID
    required_reactions: int
    watched_emojis: list[str]

    channel_overrides: list[ChannelConfigOverrideDict]
    exclude: list[ChannelID]
    exclude_is_include: bool
    allow_self_star: bool
    allow_any_emoji: bool


@dataclasses.dataclass
class StarboardChannelConfig:
    channel_id: ChannelID
    required_reactions: int
    watched_emojis: list[discord.PartialEmoji]

    # Added after-the-fact, so we need to handle the case where they're not present
    channel_overrides: dict[ChannelID, ChannelConfigOverride] = dataclasses.field(default_factory=dict)
    exclude: list[ChannelID] = dataclasses.field(default_factory=list)
    exclude_is_include: bool = False
    allow_self_star: bool = False
    allow_any_emoji: bool = False

    def is_watched(self, emoji: AnyEmoji, *, channel_id: ChannelID) -> bool:
        if self.allow_any_emoji:
            return True

        if isinstance(emoji, str):
            emoji = discord.PartialEmoji.from_str(emoji)

        watched_emojis = set(self.watched_emojis)
        if channel_id in self.channel_overrides:
            watched_emojis.update(self.channel_overrides[channel_id].extra_emojis or [])
        return any(
            watched_emoji == emoji
            for watched_emoji in watched_emojis
        )

    def relevant_required_reactions(self, channel_id: ChannelID) -> int:
        if channel_id in self.channel_overrides:
            return self.channel_overrides[channel_id].required_reactions or self.required_reactions
        return self.required_reactions


class GuildConfigs(dict[GuildID, dict[ChannelID, StarboardChannelConfig]]):
    @classmethod
    def load(cls, config_json: dict[str, list[ChannelConfigDict]]) -> Self:
        return cls({
            int(guild_id): {  # JSON does not support int keys, which is why we cast
                channel["channel_id"]: StarboardChannelConfig(
                    channel_id=channel["channel_id"],
                    required_reactions=channel["required_reactions"],
                    watched_emojis=list(map(discord.PartialEmoji.from_str, channel["watched_emojis"])),

                    channel_overrides={
                        override["override_for"]: ChannelConfigOverride(
                            override_for=override["override_for"],
                            required_reactions=override.get("required_reactions"),
                            extra_emojis=list(
                                map(discord.PartialEmoji.from_str, override["extra_emojis"]),
                            ) if override.get("extra_emojis") else None,
                            allow_self_star=override.get("allow_self_star"),
                            allow_any_emoji=override.get("allow_any_emoji"),
                        )
                        for override in channel.get("channel_overrides", [])
                    },
                    exclude=channel.get("exclude", []),
                    exclude_is_include=channel.get("exclude_is_include", False),
                    allow_self_star=channel.get("allow_self_star", False),
                    allow_any_emoji=channel.get("allow_any_emoji", False),
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
                    "watched_emojis": list(map(str, channel.watched_emojis)),
                    "channel_overrides": [
                        {
                            "override_for": override.override_for,
                            "required_reactions": override.required_reactions,
                            "extra_emojis": list(map(str, override.extra_emojis)) if override.extra_emojis else None,
                            "allow_self_star": override.allow_self_star,
                            "allow_any_emoji": override.allow_any_emoji,
                        }
                        for override in channel.channel_overrides.values()
                    ],
                    "exclude": channel.exclude,
                    "exclude_is_include": channel.exclude_is_include,
                    "allow_self_star": channel.allow_self_star,
                    "allow_any_emoji": channel.allow_any_emoji,
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
            return None
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
    add_self_star_input = discord.ui.TextInput(
        label="Allow self-starring",
        placeholder="True or False",
        required=False,
    )
    add_any_emoji_input = discord.ui.TextInput(
        label="Allow any emoji",
        placeholder="True or False",
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

        def parse_bool(value: str) -> bool | None:
            if value.lower().strip().startswith(("t", "y")):
                return True
            if value.lower().strip().startswith(("f", "n")):
                return False
            return None

        self.override = ChannelConfigOverride(
            override_for=channel_input,
            required_reactions=required_reactions,
            extra_emojis=[
                discord.PartialEmoji.from_str(emoji.strip())
                for emoji in self.extra_emojis_input.value.split(",")
            ] if self.extra_emojis_input.value else None,
            allow_self_star=parse_bool(self.add_self_star_input.value),
            allow_any_emoji=parse_bool(self.add_any_emoji_input.value),
        )
        self.stop()
        await interaction.response.defer()


class Breadboard(ModuleCog):
    command_group = app_commands.Group(
        name="starboard",
        description="Manage starboards",
        default_permissions=discord.Permissions(manage_channels=True),
        guild_only=True,
    )

    @command_group.command(
        name="add",
        description="Add a channel as a starboard",
    )
    async def starboard_add_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        required_reactions: int | None = None,
        allow_self_star: bool | None = None,
        allow_any_emoji: bool | None = None,
    ) -> None:
        if required_reactions is None:
            required_reactions = cast(int, self.settings.default_required_stars.value)
        if required_reactions <= 0:
            return await interaction.response.send_message("Required reactions must be greater than 0", ephemeral=True)
        if channel.id in self.guild_configs.get(interaction.guild_id, {}):
            return await interaction.response.send_message(
                f"Channel {channel.mention} is already a starboard. "
                "Use `/starboard modify` to change settings, or `/starboard remove` to remove it as a starboard.",
                ephemeral=True,
            )

        config = StarboardChannelConfig(
            channel_id=channel.id,
            required_reactions=required_reactions,
            watched_emojis=[
                discord.PartialEmoji.from_str(emoji)
                for emoji in cast(list[str], self.settings.default_emojis.value)
            ],
            allow_self_star=(
                allow_self_star
                if allow_self_star is not None else
                self.settings.default_allow_self_star.value
            ),
            allow_any_emoji=(
                allow_any_emoji
                if allow_any_emoji is not None else
                self.settings.default_allow_any_emoji.value
            ),
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

    @command_group.command(
        name="modify",
        description="Modify a starboard channel",
    )
    async def starboard_modify_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        required_reactions: int | None = None,
        allow_self_star: bool | None = None,
        allow_any_emoji: bool | None = None,
    ) -> None:
        if required_reactions is not None and required_reactions <= 0:
            return await interaction.response.send_message("Required reactions must be greater than 0", ephemeral=True)
        if channel.id not in self.guild_configs.get(interaction.guild_id, {}):
            return await interaction.response.send_message(
                f"Channel {channel.mention} is not a starboard. Use `/starboard add` to add it as a starboard.",
                ephemeral=True
            )

        relevant_config: StarboardChannelConfig = self.guild_configs[interaction.guild_id][channel.id]
        message = f"Modifying starboard channel {channel.mention} "
        if required_reactions is not None:
            relevant_config.required_reactions = required_reactions
            message += f"requiring {required_reactions} reactions, "
        if allow_self_star is not None:
            relevant_config.allow_self_star = allow_self_star
            message += f"{'allowing' if allow_self_star else 'disallowing'} self-starring, "
        if allow_any_emoji is not None:
            relevant_config.allow_any_emoji = allow_any_emoji
            message += f"{'allowing' if allow_any_emoji else 'disallowing'} any emoji, "
        start, sep, end = message.rstrip().removesuffix(",").rpartition(", ")
        message = f"{start} and {end}" if sep else end

        await interaction.response.send_message(
            message,
            view=ManageStarboardButtons(starboard_channel_config=relevant_config),
            ephemeral=True,
        )

    @command_group.command(
        name="remove",
        description="Remove a starboard channel",
    )
    async def starboard_remove_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if channel.id not in self.guild_configs.get(interaction.guild_id, {}):
            return await interaction.response.send_message(
                f"Channel {channel.mention} is not a starboard.",
                ephemeral=True,
            )

        del self.guild_configs[interaction.guild_id][channel.id]
        if not self.guild_configs[interaction.guild_id]:
            del self.guild_configs[interaction.guild_id]
        await interaction.response.send_message(f"Starboard channel removed: {channel.mention}", ephemeral=True)

    @command_group.command(
        name="list",
        description="List starboard channels in the current guild",
    )
    async def starboard_list_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id not in self.guild_configs:
            return await interaction.response.send_message("No starboard channels configured.", ephemeral=True)
        await interaction.response.send_message(
            "Starboard channels in this guild: "
            + ", ".join(f"<#{channel_id}>" for channel_id in self.guild_configs[interaction.guild_id]),
            ephemeral=True,
        )

    exclude_command_group = app_commands.Group(
        parent=command_group,
        name="exclude",
        description="Manage excluded channels",
        default_permissions=None,
        guild_only=True,
    )

    @exclude_command_group.command(
        name="add",
        description="Exclude a channel from a starboard",
    )
    async def starboard_exclude_add_cmd(
        self,
        interaction: discord.Interaction,
        starboard_channel: discord.TextChannel,
        exclude_channel: discord.abc.GuildChannel,
    ) -> None:
        if starboard_channel.id not in self.guild_configs.get(interaction.guild_id, {}):
            return await interaction.response.send_message(
                f"Channel {starboard_channel.mention} is not a starboard.",
                ephemeral=True,
            )
        relevant_config: StarboardChannelConfig = self.guild_configs[interaction.guild_id][starboard_channel.id]
        if exclude_channel.id in relevant_config.exclude:
            return await interaction.response.send_message(
                f"Channel {exclude_channel.mention} is already excluded from {starboard_channel.mention}",
                ephemeral=True,
            )
        relevant_config.exclude.append(exclude_channel.id)
        await interaction.response.send_message(
            f"Channel {exclude_channel.mention} excluded from {starboard_channel.mention}",
            ephemeral=True,
        )

    @exclude_command_group.command(
        name="remove",
        description="Remove a channel from the exclusion list",
    )
    async def starboard_exclude_add_cmd(
        self,
        interaction: discord.Interaction,
        starboard_channel: discord.TextChannel,
        exclude_channel: discord.abc.GuildChannel,
    ) -> None:
        if starboard_channel.id not in self.guild_configs.get(interaction.guild_id, {}):
            return await interaction.response.send_message(
                f"Channel {starboard_channel.mention} is not a starboard.",
                ephemeral=True,
            )
        relevant_config: StarboardChannelConfig = self.guild_configs[interaction.guild_id][starboard_channel.id]
        if exclude_channel.id not in relevant_config.exclude:
            return await interaction.response.send_message(
                f"Channel {exclude_channel.mention} is not excluded from {starboard_channel.mention}",
                ephemeral=True,
            )
        relevant_config.exclude.remove(exclude_channel.id)
        await interaction.response.send_message(
            f"Channel {exclude_channel.mention} removed from exclusion list of {starboard_channel.mention}",
            ephemeral=True,
        )

    @exclude_command_group.command(
        name="list",
        description="List excluded channels for a starboard",
    )
    async def starboard_exclude_list_cmd(
        self,
        interaction: discord.Interaction,
        starboard_channel: discord.TextChannel,
    ) -> None:
        if starboard_channel.id not in self.guild_configs.get(interaction.guild_id, {}):
            return await interaction.response.send_message(
                f"Channel {starboard_channel.mention} is not a starboard.",
                ephemeral=True,
            )
        relevant_config: StarboardChannelConfig = self.guild_configs[interaction.guild_id][starboard_channel.id]
        if not relevant_config.exclude:
            return await interaction.response.send_message(
                f"No channels are excluded from {starboard_channel.mention}",
                ephemeral=True,
            )
        await interaction.response.send_message(
            f"Excluded channels for {starboard_channel.mention}: "
            + ", ".join(f"<#{channel_id}>" for channel_id in relevant_config.exclude),
            ephemeral=True,
        )

    @exclude_command_group.command(
        name="invert",
        description="Invert the exclusion list to act as a whitelist",
    )
    async def starboard_exclude_invert_cmd(
        self,
        interaction: discord.Interaction,
        starboard_channel: discord.TextChannel,
        is_whitelist: bool | None = None,
    ) -> None:
        if starboard_channel.id not in self.guild_configs.get(interaction.guild_id, {}):
            return await interaction.response.send_message(
                f"Channel {starboard_channel.mention} is not a starboard.",
                ephemeral=True,
            )
        relevant_config: StarboardChannelConfig = self.guild_configs[interaction.guild_id][starboard_channel.id]
        if is_whitelist is None:
            is_whitelist = not relevant_config.exclude_is_include

        relevant_config.exclude_is_include = is_whitelist
        await interaction.response.send_message(
            f"Exclusion list for {starboard_channel.mention} now acts as a "
            + ("whitelist" if is_whitelist else "blacklist"),
            ephemeral=True,
        )

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
            json.dump(self.guild_configs.dump(), f, indent=4, ensure_ascii=False)

    @staticmethod
    def setup_db(connection: sqlite3.Connection) -> None:
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
            if config.is_watched(reaction_event.emoji, channel_id=reaction_event.channel_id)
        ]
        channel = (
            self.bot.get_channel(reaction_event.channel_id)
            or await self.bot.fetch_channel(reaction_event.channel_id)
        )
        if channel:
            relevant_configs = [
                config
                for config in relevant_configs
                if (channel.id not in config.exclude) ^ config.exclude_is_include
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
            emoji: [
                user
                for user in users
                if channel_config.allow_self_star or user.id != message.author.id
            ]
            for emoji, users in reaction_map.items()
            if channel_config.is_watched(emoji, channel_id=message.channel.id)
        }
        unique_reaction_count: int = len({user for users in relevant_reaction_map.values() for user in users})

        sql_response: tuple[MessageID, int] | None = self.connection.execute(
            "SELECT starboard_message_id, star_count FROM starred_messages WHERE original_id = ?",
            (message.id,),
        ).fetchone()

        if unique_reaction_count >= channel_config.relevant_required_reactions(message.channel.id):
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
        unique_reaction_count: int = len({user for users in relevant_reaction_map.values() for user in users})

        embeds: list[discord.Embed] = [embed for embed in message.embeds if embed.type == "rich"]
        files: list[discord.File] = [await attachment.to_file() for attachment in message.attachments]

        if referencing:
            reply_text = f"-# Replying to {referencing.author.mention}\n"
            for line in referencing.content.splitlines():
                line = line.removeprefix('-# ')
                reply_text += f"> -# {line}\n" if line else "> _ _\n"
            reply_text += "\n"

            if len(reply_text + message.content) <= 2000:
                message.content = reply_text + message.content

        if message.stickers:
            for sticker in message.stickers:
                files.append(await sticker.to_file())

        webhook_msg = await webhook.send(
            username=message.author.display_name,
            avatar_url=avatar.url if (avatar := message.author.avatar) else None,
            content=message.content,
            embeds=embeds[:10],
            files=files[:10],
            allowed_mentions=discord.AllowedMentions.none(),
            view=OriginalMessageButton(
                original_message_url=message.jump_url,
                star_count=unique_reaction_count,
                star_emoji=get_top_emoji(relevant_reaction_map),
            ),
            wait=True,
        )

        try:
            self.connection.execute(
                "INSERT INTO starred_messages (original_id, starboard_message_id, starboard_channel_id, star_count) "
                "VALUES (?, ?, ?, ?)",
                (message.id, webhook_msg.id, config.channel_id, unique_reaction_count),
            )
        except sqlite3.IntegrityError:  # Message was already starred, this resolves a race condition
            await webhook.delete_message(webhook_msg.id)
        else:
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
