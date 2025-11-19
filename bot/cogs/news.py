"""
News Cog - Discord bot commands for news fetching with real-time updates
"""
import discord
from discord.ext import commands, tasks
import logging
from news_sources.fetcher import NewsFetcher
from utils.embeds import create_article_embed, create_help_embed
import os
import json
import asyncio
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class News(commands.Cog):
    """Real-time news fetching and posting"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.news_fetcher = NewsFetcher(newsapi_key=os.getenv("NEWS_API_KEY"))
        self.posted_articles = {}  # URL -> timestamp of when it was posted
        self.last_check_time = {}  # region -> last article timestamp
        self.config = self.load_config()
        self.load_posted_articles()
        
        # Start the real-time news checker
        self.real_time_news_checker.start()
        logger.info("News bot initialized with real-time updates")
    
    def load_config(self):
        """Load configuration from config.json"""
        try:
            if os.path.exists("config.json"):
                with open("config.json", "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error loading config.json: {e}")
        return {}

    def load_posted_articles(self):
        """Load previously posted article URLs from cache file"""
        try:
            if os.path.exists("posted_articles.json"):
                with open("posted_articles.json", "r") as f:
                    data = json.load(f)
                    self.posted_articles = {k: v for k, v in data.items()}
                logger.info(f"Loaded {len(self.posted_articles)} previously posted articles")
        except Exception as e:
            logger.error(f"Error loading posted articles: {e}")
            self.posted_articles = {}

    def save_posted_articles(self):
        """Save posted article URLs to cache file"""
        try:
            with open("posted_articles.json", "w") as f:
                json.dump(self.posted_articles, f)
        except Exception as e:
            logger.error(f"Error saving posted articles: {e}")

    def cleanup_old_articles(self):
        """Remove articles older than 7 days from cache"""
        try:
            now = datetime.now()
            old_articles = [
                url for url, timestamp_str in self.posted_articles.items()
                if datetime.fromisoformat(timestamp_str) < now - timedelta(days=7)
            ]
            for url in old_articles:
                del self.posted_articles[url]
            if old_articles:
                logger.info(f"Cleaned up {len(old_articles)} old articles")
                self.save_posted_articles()
        except Exception as e:
            logger.error(f"Error cleaning up old articles: {e}")

    @commands.command(name="news")
    async def fetch_news_command(self, ctx: commands.Context, region: str = "all"):
        """
        Get news from a specific region on-demand
        
        Usage: !news [usa|uk|europe|all]
        """
        region = region.lower()
        
        if region not in ["usa", "uk", "europe", "all"]:
            await ctx.send("Invalid region. Use: usa, uk, europe, or all")
            return
        
        async with ctx.typing():
            try:
                news_data = await asyncio.wait_for(
                    self.news_fetcher.fetch_all_news(),
                    timeout=90
                )
                
                if region == "all":
                    for reg, articles in news_data.items():
                        if articles:
                            for article in articles[:3]:
                                embed = create_article_embed(article)
                                await ctx.send(embed=embed)
                else:
                    articles = news_data.get(region, [])
                    if not articles:
                        await ctx.send(f"No news found for {region}")
                        return
                    
                    for article in articles[:5]:
                        embed = create_article_embed(article)
                        await ctx.send(embed=embed)
                
                logger.info(f"Sent news for region: {region} to user {ctx.author}")
                
            except asyncio.TimeoutError:
                logger.error(f"Timeout fetching news for region {region}")
                await ctx.send(f"Timeout fetching news. Please try again later.")
            except Exception as e:
                logger.error(f"Error fetching news: {e}", exc_info=True)
                await ctx.send(f"Error fetching news: {str(e)}")

    @commands.command(name="bothelp")
    async def show_help(self, ctx: commands.Context):
        """Show available commands"""
        embed = create_help_embed()
        await ctx.send(embed=embed)

    @tasks.loop(minutes=2)
    async def real_time_news_checker(self):
        """
        Check for new articles every 2 minutes and post immediately
        Real-time news streaming to Discord channel
        """
        try:
            logger.debug("Checking for new articles...")
            
            # Get configured channel ID from config.json or environment
            channel_id = self.config.get("discord", {}).get("channel_id") or os.getenv("DISCORD_CHANNEL_ID")
            if not channel_id or channel_id == "your_channel_id_here":
                logger.debug("DISCORD_CHANNEL_ID not configured, skipping auto-post")
                return
            
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                logger.warning(f"Channel {channel_id} not found")
                return
            
            # Fetch latest news with error handling
            try:
                news_data = await asyncio.wait_for(
                    self.news_fetcher.fetch_all_news(),
                    timeout=90  # 90 second timeout for entire fetch operation
                )
            except asyncio.TimeoutError:
                logger.error("News fetcher timed out after 90 seconds, skipping this cycle")
                return
            except Exception as e:
                logger.error(f"Error fetching news: {e}", exc_info=True)
                return
            
            new_articles_posted = 0
            
            for region, articles in news_data.items():
                if not articles:  # Skip empty regions
                    continue
                    
                for article in articles[:10]:  # Check top 10 articles per region
                    # Check if this article was already posted
                    if article.url not in self.posted_articles:
                        try:
                            # Send article to Discord channel
                            embed = create_article_embed(article)
                            await channel.send(embed=embed)
                            
                            # Mark as posted with timestamp
                            self.posted_articles[article.url] = datetime.now().isoformat()
                            new_articles_posted += 1
                            
                            logger.info(
                                f"[{region.upper()}] NEW ARTICLE: {article.title[:60]}... "
                                f"| Source: {article.source}"
                            )
                            
                            # Small delay between posts to avoid rate limiting
                            await asyncio.sleep(0.5)
                            
                        except discord.errors.HTTPException as e:
                            if e.status == 429:  # Rate limited
                                logger.warning("Discord rate limit hit, slowing down...")
                                await asyncio.sleep(5)
                            else:
                                logger.error(f"Error posting article: {e}")
            
            if new_articles_posted > 0:
                self.save_posted_articles()
                logger.info(f"Posted {new_articles_posted} new articles in this cycle")
                
                # Cleanup old articles periodically (every 50 cycles = ~100 minutes)
                if len(self.posted_articles) % 50 == 0:
                    self.cleanup_old_articles()
                
        except Exception as e:
            logger.error(f"Error in real-time news checker: {e}", exc_info=True)

    @real_time_news_checker.before_loop
    async def before_real_time_checker(self):
        """Wait for bot to be ready before starting real-time checker"""
        await self.bot.wait_until_ready()

    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        if hasattr(self, 'real_time_news_checker'):
            self.real_time_news_checker.cancel()

async def setup(bot: commands.Bot):
    """Setup the News cog"""
    await bot.add_cog(News(bot))
