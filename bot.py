import asyncio
import functools
import itertools
import math
import random

import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands
from config import token


class VoiceError(Exception):
    pass


class YTDLError(Exception):
    pass


# ID сервера для приветствия
guild_welcome = {962314560382050314}


# параметры для скачивания песни
class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }

    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1'
                          ' -reconnect_streamed 1'
                          ' -reconnect_delay_max 5',
        'options': '-vn',
    }

    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx, source: discord.FFmpegPCMAudio, *,
                 data: dict, volume: float = 0.5):
        super().__init__(source, volume)

        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data

        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return '**{0.title}** by **{0.uploader}**'.format(self)

    # создание ссылки для воспроизведения
    @classmethod
    async def create_source(cls, ctx, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()

        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        # cls.ytdl.extract_info - метод для поиска видео на ютуб по ключевому слову
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise YTDLError(f'Не удалось найти ничего подходящего {search}')

        if 'entries' not in data:
            process_info = data
        else:
            process_info = None
            for entry in data['entries']:
                if entry:
                    process_info = entry
                    break

            if process_info is None:
                raise YTDLError(f'Не удалось найти ничего подходящего {search}')

        webpage_url = process_info['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed_info = await loop.run_in_executor(None, partial)

        if processed_info is None:
            raise YTDLError(f'Не удалось получить `{webpage_url}`')

        if 'entries' not in processed_info:
            info = processed_info
        else:
            info = None
            while info is None:
                try:
                    info = processed_info['entries'].pop(0)
                except IndexError:
                    raise YTDLError(f'Не удалось найти ничего подходящего {webpage_url}')

        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    # длительность трека
    @staticmethod
    def parse_duration(duration):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        duration = []
        if days > 0:
            duration.append(f'{days} дни')
        if hours > 0:
            duration.append(f'{hours} часы')
        if minutes > 0:
            duration.append(f'{minutes} минутs')
        if seconds > 0:
            duration.append(f'{seconds} секунд')

        return ', '.join(duration)


# название песни , исполнитель и тд выводятся в чат
class Song:
    __slots__ = ('source', 'requester')

    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = (discord.Embed(title='Сейчас играет',
                               description='```css\n{0.source.title}\n```'.format(self),
                               color=discord.Color.blurple())
                 .add_field(name='Продолжительность', value=self.source.duration)
                 .add_field(name='Поставлена', value=self.requester.mention)
                 .add_field(name='Имполнитель', value='[{0.source.uploader}]'
                                                      '({0.source.uploader_url})'.format(self))
                 .add_field(name='Ссылка на трек', value='[Click]({0.source.url})'.format(self))
                 .set_thumbnail(url=self.source.thumbnail))

        return embed


# очередь
class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start, item.stop, item.step))
        else:
            return self._queue[item]

    def __iter__(self):
        return self._queue.__iter__()

    def __len__(self):
        return self.qsize()

    def clear(self):
        self._queue.clear()

    def shuffle(self):
        random.shuffle(self._queue)

    def remove(self, index: int):
        del self._queue[index]


class VoiceState:
    def __init__(self, bot, ctx):
        self.bot = bot
        self._ctx = ctx

        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()

        self._loop = False
        self._volume = 0.5
        self.skip_votes = set()

        self.audio_player = bot.loop.create_task(self.audio_player_task())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def loop(self):
        return self._loop

    @loop.setter
    def loop(self, value: bool):
        self._loop = value

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value: float):
        self._volume = value

    @property
    def is_playing(self):
        return self.voice and self.current

    async def audio_player_task(self):
        while True:
            self.next.clear()

            if not self.loop:
                try:
                    async with timeout(180):
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    self.bot.loop.create_task(self.stop())
                    return

            self.current.source.volume = self._volume
            self.voice.play(self.current.source, after=self.play_next_song)
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    def play_next_song(self, error=None):
        if error:
            raise VoiceError(str(error))

        self.next.set()

    def skip(self):
        self.skip_votes.clear()

        if self.is_playing:
            self.voice.stop()

    async def stop(self):
        self.songs.clear()

        if self.voice:
            await self.voice.disconnect()
            self.voice = None


# команды бот для работы с музыкой
class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, ctx):
        state = self.voice_states.get(ctx.guild.id)
        if not state:
            state = VoiceState(self.bot, ctx)
            self.voice_states[ctx.guild.id] = state
        return state

    def cog_unload(self):
        for state in self.voice_states.values():
            self.bot.loop.create_task(state.stop())

    async def cog_before_invoke(self, ctx):
        ctx.voice_state = self.get_voice_state(ctx)

    async def cog_command_error(self, ctx, error: commands.CommandError):
        await ctx.send(f'Ошибка: {str(error)}')

    # вызывает бота в голосовой канал где вы находлитесь
    @commands.command(name='join', invoke_without_subcommand=True)
    async def _join(self, ctx):
        """Присоеденится к каналу"""

        destination = ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()
        await ctx.send('Бот готов к труду и обороне')

    # вызывает бота в голосовой канал где вы находлитесь, доступна только владельцам серевера
    @commands.command(name='summon')
    @commands.has_permissions(manage_guild=True)
    async def _summon(self, ctx, *, channel: discord.VoiceChannel = None):
        """Вызывает бота в голосовой канал"""

        if not channel and not ctx.author.voice:
            raise VoiceError('Вы не подключены к голосовому каналу.')

        destination = channel or ctx.author.voice.channel
        if ctx.voice_state.voice:
            await ctx.voice_state.voice.move_to(destination)
            return

        ctx.voice_state.voice = await destination.connect()
        ctx.send('Бот готов к труду и обороне')

    # бот очищает очередь и покидает голосовой канал
    @commands.command(name='leave', aliases=['disconnect'])
    async def _leave(self, ctx):
        """Очищает очередь и покидает голосовой канал"""

        if not ctx.voice_state.voice:
            return await ctx.send('Не подключен ни к одному голосовому каналу.')

        await ctx.voice_state.stop()
        del self.voice_states[ctx.guild.id]

    # отображает текущую песню
    @commands.command(name='now', aliases=['current', 'playing'])
    async def _now(self, ctx):
        """Отображает текущую песню"""

        await ctx.send(embed=ctx.voice_state.current.create_embed())

    # приостанавливает текущую песню
    @commands.command(name='pause')
    async def _pause(self, ctx):
        """Приостанавливает текущую песню"""

        if ctx.voice_state.voice.is_playing():
            ctx.voice_state.voice.pause()

    # возобновляет приостановленную в данный момент песню
    @commands.command(name='resume')
    async def _resume(self, ctx):
        """Возобновляет приостановленную в данный момент песню"""

        if ctx.voice_state.voice.is_paused():
            ctx.voice_state.voice.resume()

    # останавливает воспроизведение песни и очищает очередь
    @commands.command(name='stop')
    async def _stop(self, ctx):
        """Останавливает воспроизведение песни и очищает очередь"""

        ctx.voice_state.songs.clear()
        ctx.voice_state.voice.stop()

    # пропуск песни
    @commands.command(name='next')
    async def _skip(self, ctx):
        """Следующая песня"""

        if not ctx.voice_state.is_playing:
            return await ctx.send('Музыку сейчас не слушают')

        ctx.voice_state.skip()

    # показывает очередь
    @commands.command(name='line')
    async def _queue(self, ctx, *, page: int = 1):
        """Показывает очередь бота."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Очередь пуста')

        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} трека:**\n\n{}'
                               .format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Просмотр страницы {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    # перемешивание очереди, посе перемешивания показывает очередь
    @commands.command(name='mix')
    async def _shuffle(self, ctx, page: int = 1):
        """Перемешивает очередь"""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Очередь пуста')

        ctx.voice_state.songs.shuffle()
        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} трека:**\n\n{}'
                               .format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Просмотр страницы {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    # удаление музыки из очереди, посе удаления показывает очередь
    @commands.command(name='delete')
    async def _delete(self, ctx, index: int, page: int = 1):
        """Удаляет песню из очереди по заданному индексу."""

        if len(ctx.voice_state.songs) == 0:
            return await ctx.send('Очередь пуста')

        ctx.voice_state.songs.remove(index - 1)
        items_per_page = 10
        pages = math.ceil(len(ctx.voice_state.songs) / items_per_page)

        start = (page - 1) * items_per_page
        end = start + items_per_page

        queue = ''
        for i, song in enumerate(ctx.voice_state.songs[start:end], start=start):
            queue += '`{0}.` [**{1.source.title}**]({1.source.url})\n'.format(i + 1, song)

        embed = (discord.Embed(description='**{} трека:**\n\n{}'
                               .format(len(ctx.voice_state.songs), queue))
                 .set_footer(text='Просмотр страницы {}/{}'.format(page, pages)))
        await ctx.send(embed=embed)

    # команда, проигрывающая музыку
    @commands.command(name='play')
    async def _play(self, ctx, *, search: str):
        """Играет песня.
        Если в очереди есть песни, они будут поставлены в очередь до тех пор, пока
        другие песни закончили играть."""

        if not ctx.voice_state.voice:
            await ctx.invoke(self._join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search)
            except YTDLError as e:
                await ctx.send(f'При обработке этого запроса произошла ошибка: {str(e)}')
            else:
                song = Song(source)

                await ctx.voice_state.songs.put(song)
                await ctx.send(f'В очереди {str(source)}')

    # ошибки, если пользователь вызывает бота , не находясь в войсе
    @_join.before_invoke
    @_play.before_invoke
    async def ensure_voice_state(self, ctx):
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise commands.CommandError('Вы не подключены ни к одному голосовому каналу')

        if ctx.voice_client:
            if ctx.voice_client.channel != ctx.author.voice.channel:
                raise commands.CommandError('Бот уже в голосовом канале')


# префикс
bot = commands.Bot('!')
bot.add_cog(Music(bot))


# бот отправляет в консоль информацию, что он запустился
@bot.event
async def on_ready():
    print('Бот в сети и готoв работать')


# приветствие нового участника
@bot.event
async def on_member_join(member):
    welcome = bot.get_channel(guild_welcome[member.guild.id])
    embed = discord.Embed(title='Добро пожаловать!',
                          description=f'К нам в {member.guild.name} 'f'приехал {member.mention}!',
                          color=0xCC974F)
    await welcome.send(embed=embed)


bot.run(token)
