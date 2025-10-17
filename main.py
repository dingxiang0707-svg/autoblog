#!/usr/bin/env python3
"""
McKinsey文章抓取API服务器
提供HTTP接口供n8n调用，返回文章内容
"""

import time
import json
import os
import zipfile
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import traceback
import logging
from logging.handlers import RotatingFileHandler
from playwright.sync_api import sync_playwright

app = Flask(__name__)
CORS(app)

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not os.path.exists('logs'):
    os.makedirs('logs')
    
file_handler = RotatingFileHandler('logs/mckinsey_api.log', maxBytes=10240000, backupCount=10)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
))
file_handler.setLevel(logging.INFO)
app.logger.addHandler(file_handler)

class McKinseyScraperAPI:
    def __init__(self):
        # API服务器的工作目录
        self.work_dir = os.path.abspath(os.path.dirname(__file__))
        self.static_dir = os.path.join(self.work_dir, 'static')
        self.files_dir = os.path.join(self.static_dir, 'files')
        
        # 历史文件
        self.links_file = os.path.join(self.work_dir, 'latest_two_articles.json')
        
        # N8N监控的输出文件夹
        self.n8n_output_dir = os.path.join(self.static_dir, 'mckinsey_output')
        self.ensure_directories()
        
        self.base_url = "https://www.mckinsey.com/capabilities/quantumblack/our-insights"
    
    def ensure_directories(self):
        """确保所需目录存在"""
        for directory in [self.static_dir, self.files_dir, self.n8n_output_dir]:
            if not os.path.exists(directory):
                os.makedirs(directory)
                logger.info(f"✅ 创建目录: {directory}")
    
    def clean_old_files(self, max_age_hours=24):
        """清理超过指定时间的旧文件"""
        try:
            current_time = time.time()
            max_age_seconds = max_age_hours * 3600
            
            for directory in [self.files_dir, self.n8n_output_dir]:
                for root, dirs, files in os.walk(directory):
                    for file in files:
                        file_path = os.path.join(root, file)
                        if current_time - os.path.getctime(file_path) > max_age_seconds:
                            try:
                                os.remove(file_path)
                                logger.info(f"🗑️ 清理旧文件: {file_path}")
                            except Exception as e:
                                logger.error(f"清理文件失败 {file_path}: {e}")
                    
                    # 清理空目录
                    for dir_name in dirs:
                        dir_path = os.path.join(root, dir_name)
                        try:
                            if not os.listdir(dir_path):
                                os.rmdir(dir_path)
                                logger.info(f"🗑️ 清理空目录: {dir_path}")
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"清理旧文件时出错: {e}")

    def parse_date_for_sorting(self, date_str):
        """解析日期用于排序"""
        if not date_str or date_str == "未找到时间":
            return datetime(1900, 1, 1)
        
        try:
            # 处理 "September 10, 2025" 格式
            return datetime.strptime(date_str, "%B %d, %Y")
        except:
            try:
                # 处理其他可能的格式
                return datetime.strptime(date_str, "%Y-%m-%d")
            except:
                return datetime(1900, 1, 1)

    def load_existing_articles(self):
        """加载已存在的文章URL和最新日期"""
        existing_urls = set()
        latest_date = datetime(1900, 1, 1)  # 初始化为很久之前的日期
        all_historical_articles = []
        
        if os.path.exists(self.links_file):
            try:
                with open(self.links_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    # 获取历史文章列表
                    if 'latest_two_articles' in data:
                        all_historical_articles = data['latest_two_articles']
                    elif isinstance(data, list):
                        all_historical_articles = data
                    
                    # 收集所有URL并找到最新日期
                    for article in all_historical_articles:
                        existing_urls.add(article['url'])
                        article_date = self.parse_date_for_sorting(article.get('date', ''))
                        if article_date > latest_date:
                            latest_date = article_date
                            
                logger.info(f"从 {self.links_file} 加载了 {len(existing_urls)} 个已存在的文章URL")
                logger.info(f"最新文章日期: {latest_date.strftime('%B %d, %Y') if latest_date.year > 1900 else '无历史记录'}")
                
            except Exception as e:
                logger.error(f"加载 {self.links_file} 失败: {e}")
        else:
            logger.info("未找到历史文件，这是第一次运行")
        
        return existing_urls, latest_date, all_historical_articles

    def is_valid_article(self, href, title, full_href):
        """
        改进的文章验证逻辑 - 精确筛选掉非文章链接
        """
        # 基本URL格式检查
        if not href or not title:
            return False
        
        # 排除明显的非文章链接
        excluded_patterns = [
            'app-store', 'play.google', 'apple.com',
            '/careers', '/contact-us', '/search',
            '/subscribe', '/newsletter', '/events', '/privacy',
            '/terms', '/cookie', '/accessibility',
            'linkedin.com', 'twitter.com', 'facebook.com'
        ]
        
        # 检查URL排除模式
        for pattern in excluded_patterns:
            if pattern in full_href.lower():
                return False
        
        # 定义非文章标题的黑名单
        title_blacklist_exact = [
            'read the article', 'read more', 'learn more',
            'view article', 'see more', 'continue reading',
            'contact us', 'contact', 'scam warning', 'terms of use',
            'local language information', 'accessibility statement',
            'cookie notice', 'privacy notice', 'privacy policy',
            'more menu options', 'subscribe', 'newsletter',
            'more', 'menu', 'search', 'login', 'sign up', 'register',
            'home', 'about', 'careers'
        ]
        
        # 部分匹配的黑名单关键词
        title_blacklist_partial = [
            'contact us', 'scam warning', 'terms of use',
            'local language information', 'accessibility statement', 
            'cookie notice', 'privacy notice', 'more menu options',
            'subscribe', 'newsletter'
        ]
        
        title_lower = title.lower().strip()
        
        # 检查完整匹配
        if title_lower in title_blacklist_exact:
            return False
        
        # 检查部分匹配
        if any(blacklisted in title_lower for blacklisted in title_blacklist_partial):
            return False
        
        # 排除过于简短或通用的标题
        if len(title.split()) <= 2 and len(title.strip()) < 30:
            return False
        
        # 排除以省略号结尾的简短标题
        if title.strip().endswith('...') and len(title.strip()) < 30:
            return False
        
        min_title_length = 15
        
        # 检查是否包含文章相关路径
        article_indicators = [
            '/our-insights/', '/capabilities/', '/industries/', 
            '/featured-insights/', '/blog/', '/article/',
            '/about-us/new-at-mckinsey-blog/'
        ]
        
        has_article_path = any(indicator in full_href.lower() for indicator in article_indicators)
        
        # 确保URL段落足够长
        url_segments = href.split('/')
        last_segment = url_segments[-1] if url_segments else ""
        has_meaningful_url = len(last_segment) > 10
        
        # 最终判断条件
        is_valid = (
            len(title.strip()) >= min_title_length and
            has_article_path and
            has_meaningful_url and
            not title.lower().startswith(('http', 'www', 'click'))
        )
        
        return is_valid

    def extract_latest_articles(self):
        """提取最新的文章链接（只要比上次更新的）"""
        logger.info("🔍 步骤1: 提取McKinsey最新文章链接...")
        
        # 加载已存在的文章和最新日期
        existing_urls, latest_date, all_historical_articles = self.load_existing_articles()
        logger.info(f"已存在文章数量: {len(existing_urls)}")
        
        with sync_playwright() as p:
            browser = p.firefox.launch(
                headless=True,
                firefox_user_prefs={
                    "dom.webdriver.enabled": False,
                    "useAutomationExtension": False,
                }
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale='en-US',
                timezone_id='America/New_York'
            )
            page = context.new_page()
            
            # 隐藏 webdriver 特征
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)
            
            try:
                logger.info(f"访问页面: {self.base_url}")
                
                page.goto(
                    self.base_url, 
                    wait_until="domcontentloaded",
                    timeout=60000  # 60秒超时
                )
                time.sleep(8)  # 等待页面完全加载
                
                logger.info(f"页面标题: {page.title()}")
                
                # 尝试滚动页面加载更多内容
                logger.info("滚动页面加载更多内容...")
                for i in range(3):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(2)
                    logger.info(f"  滚动 {i+1}/3...")
                
                # 获取所有可能的文章链接
                logger.info("获取文章链接...")
                
                link_selectors = [
                    "a[href*='/our-insights/']",
                    "a[href*='/capabilities/']", 
                    "a[href*='/industries/']",
                    "a[href*='/featured-insights/']",
                    "a[data-component='mdc-c-link']",
                    "a[href*='mckinsey.com']",
                    "a[class*='mdc-c-link']"
                ]
          
                all_articles = []
                
                for selector in link_selectors:
                    try:
                        links = page.query_selector_all(selector)
                        logger.info(f"选择器 '{selector}': 找到 {len(links)} 个链接")
                        
                        for link in links:
                            href = link.get_attribute("href")
                            title = link.inner_text().strip()
                            
                            if href and title:
                                # 标准化URL
                                if href.startswith("/"):
                                    full_href = "https://www.mckinsey.com" + href
                                else:
                                    full_href = href
                                
                                # 使用改进的筛选逻辑
                                is_real_article = self.is_valid_article(href, title, full_href)
                                
                                if is_real_article:
                                    # 避免重复
                                    if not any(article['url'] == full_href for article in all_articles):
                                        all_articles.append({
                                            "title": title,
                                            "url": full_href,
                                            "date": None,
                                            "snippet": ""
                                        })
                                        logger.info(f"  发现文章: {title[:60]}...")
                            
                    except Exception as e:
                        logger.error(f"选择器 '{selector}' 失败: {e}")
                
                logger.info(f"总共找到 {len(all_articles)} 篇文章")
                
                # 重新获取文章和时间信息，确保匹配正确
                logger.info("重新获取文章和时间信息以确保正确匹配...")
                
                # 查找文章容器，从容器中同时提取文章和时间
                article_containers = page.query_selector_all("[class*='GenericItem'], [data-component*='generic-item'], .mdc-c-generic-item")
                logger.info(f"找到 {len(article_containers)} 个文章容器")
                
                matched_articles = []
                
                for i, container in enumerate(article_containers):
                    try:
                        # 在容器内查找链接
                        links = container.query_selector_all("a")
                        article_link = None
                        article_title = None
                        
                        for link in links:
                            href = link.get_attribute("href")
                            title = link.inner_text().strip()
                            
                            if href and title and len(title) > 10:
                                # 标准化URL
                                if href.startswith("/"):
                                    full_href = "https://www.mckinsey.com" + href
                                else:
                                    full_href = href
                                
                                # 检查是否是有效文章
                                if self.is_valid_article(href, title, full_href):
                                    article_link = full_href
                                    article_title = title
                                    break
                        
                        if article_link and article_title:
                            # 在同一个容器内查找时间信息
                            date_elem = container.query_selector(".GenericItem_mck-c-generic-item__display-date__79HZa, [class*='date'], time")
                            date_text = "未找到时间"
                            
                            if date_elem:
                                try:
                                    date_text = date_elem.inner_text().strip()
                                    if date_text and date_text.endswith(' -'):
                                        date_text = date_text[:-2].strip()
                                    if not date_text:
                                        date_text = "未找到时间"
                                except:
                                    date_text = "获取失败"
                            
                            # 检查是否已经存在（避免重复）
                            if not any(art['url'] == article_link for art in matched_articles):
                                matched_articles.append({
                                    "title": article_title,
                                    "url": article_link,
                                    "date": date_text,
                                    "snippet": ""
                                })
                                logger.info(f"{len(matched_articles)}. {article_title[:50]}... - {date_text}")
                    
                    except Exception as e:
                        logger.error(f"处理容器 {i+1} 失败: {e}")
                        continue
                
                # 如果匹配的文章数量不够，使用原来的方法作为补充
                if len(matched_articles) < len(all_articles):
                    logger.info(f"匹配文章数 ({len(matched_articles)}) 少于总文章数 ({len(all_articles)})，使用补充方法...")
                    
                    # 为未匹配的文章分配剩余时间
                    all_date_elements = page.query_selector_all(".GenericItem_mck-c-generic-item__display-date__79HZa")
                    remaining_dates = []
                    for date_elem in all_date_elements:
                        try:
                            date_text = date_elem.inner_text().strip()
                            if date_text and date_text.endswith(' -'):
                                date_text = date_text[:-2].strip()
                            if date_text:
                                remaining_dates.append(date_text)
                        except:
                            remaining_dates.append("获取失败")
                    
                    # 补充未匹配的文章
                    matched_urls = {art['url'] for art in matched_articles}
                    date_index = len(matched_articles)
                    
                    for article in all_articles:
                        if article['url'] not in matched_urls:
                            if date_index < len(remaining_dates):
                                article['date'] = remaining_dates[date_index]
                            else:
                                article['date'] = "未找到时间"
                            matched_articles.append(article)
                            logger.info(f"{len(matched_articles)}. {article['title'][:50]}... - {article['date']}")
                            date_index += 1
                
                # 使用匹配后的文章列表
                all_articles = matched_articles
                logger.info(f"最终匹配到 {len(all_articles)} 篇文章")
                
                # 过滤新文章
                newer_articles = []
                for article in all_articles:
                    article_date = self.parse_date_for_sorting(article['date'])
                    
                    if article['url'] not in existing_urls and article_date > latest_date:
                        # 再次应用筛选逻辑确保是真正的文章
                        href = article['url'].replace('https://www.mckinsey.com', '')
                        if self.is_valid_article(href, article['title'], article['url']):
                            newer_articles.append(article)
                            logger.info(f"✅ 新文章: {article['title'][:50]}... ({article['date']})")
                        else:
                            logger.info(f"❌ 过滤非文章: {article['title'][:50]}... ({article['date']})")
                    else:
                        if article['url'] in existing_urls:
                            logger.info(f"⏭️ 跳过重复: {article['title'][:50]}...")
                        elif article_date <= latest_date:
                            logger.info(f"⏭️ 跳过旧文章: {article['title'][:50]}... ({article['date']})")
                
                logger.info(f"找到 {len(newer_articles)} 篇新文章")
                
                # 按日期排序，获取最新的两篇
                newer_articles.sort(key=lambda x: self.parse_date_for_sorting(x['date']), reverse=True)
                latest_two = newer_articles[:2]
                
                if latest_two:
                    logger.info(f"本次最新的文章:")
                    for i, article in enumerate(latest_two, 1):
                        logger.info(f"{i}. {article['title']}")
                        logger.info(f"   时间: {article['date']}")
                        logger.info(f"   链接: {article['url']}")
                    
                    # 将新文章追加到历史记录中
                    updated_articles = all_historical_articles + latest_two
                    
                    # 保存更新后的完整列表
                    result = {
                        "extraction_time": datetime.now().isoformat(),
                        "total_articles_found": len(all_articles),
                        "new_articles_found": len(latest_two),
                        "total_historical_count": len(updated_articles),
                        "latest_two_articles": updated_articles
                    }
                    
                    with open(self.links_file, "w", encoding="utf-8") as f:
                        json.dump(result, f, indent=2, ensure_ascii=False)
                    
                    logger.info("✅ 新文章已追加到历史记录!")
                    logger.info(f"总历史文章数: {len(updated_articles)}")
                    
                else:
                    logger.info("❌ 没有找到比上次更新的文章")
                    result = {
                        "extraction_time": datetime.now().isoformat(),
                        "total_articles_found": len(all_articles),
                        "new_articles_found": 0,  # 明确标记为0
                        "total_historical_count": len(all_historical_articles),
                        "latest_two_articles": all_historical_articles
                    }
                    
                    with open(self.links_file, "w", encoding="utf-8") as f:
                        json.dump(result, f, indent=2, ensure_ascii=False)
                
                logger.info(f"💾 结果已保存到: {self.links_file}")
                
                browser.close()
                return latest_two
                
            except Exception as e:
                logger.error(f"❌ 提取失败: {e}")
                browser.close()
                return []

    def load_article_links(self):
        """从JSON文件加载需要抓取内容的文章链接"""
        if not os.path.exists(self.links_file):
            logger.error(f"❌ 未找到链接文件: {self.links_file}")
            return []
        
        try:
            with open(self.links_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 检查是否有新文章
            new_articles_count = data.get('new_articles_found', 0)
            
            if new_articles_count == 0:
                logger.info("🚫 没有新文章需要抓取，跳过内容抓取步骤")
                return []
            
            articles = []
            if 'latest_two_articles' in data:
                all_articles = data['latest_two_articles']
                
                # 只获取最新添加的文章
                if new_articles_count > 0:
                    articles = all_articles[-new_articles_count:]
                    logger.info(f"✅ 发现 {new_articles_count} 篇新文章需要抓取内容")
            
            for i, article in enumerate(articles, 1):
                logger.info(f"{i}. {article.get('title', '未知标题')}")
                logger.info(f"   链接: {article.get('url', '')}")
                logger.info(f"   日期: {article.get('date', '未知日期')}")
            
            return articles
            
        except Exception as e:
            logger.error(f"❌ 加载链接文件失败: {e}")
            return []

    def scrape_article_content(self, article_url):
        """抓取单篇文章内容"""
        with sync_playwright() as p:
            browser = p.firefox.launch(
                headless=True,
                firefox_user_prefs={
                    "dom.webdriver.enabled": False,
                    "useAutomationExtension": False,
                }
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale='en-US',
                timezone_id='America/New_York'
            )
            
            # 设置额外的浏览器属性来避免被检测
            page = context.new_page()
            
            # 隐藏 webdriver 特征
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)

            try:
                logger.info(f"🌐 访问文章: {article_url}")
                
                # 增加超时时间到 60 秒，并使用重试机制
                max_retries = 3
                retry_delay = 5
                
                for attempt in range(max_retries):
                    try:
                        logger.info(f"尝试 {attempt + 1}/{max_retries}...")
                        page.goto(
                            article_url, 
                            wait_until="domcontentloaded",
                            timeout=60000  # 60秒超时
                        )
                        page.wait_for_timeout(8000)
                        break  # 成功则跳出循环
                    except Exception as e:
                        if attempt < max_retries - 1:
                            logger.warning(f"⚠️ 第 {attempt + 1} 次尝试失败: {e}")
                            logger.info(f"⏰ 等待 {retry_delay} 秒后重试...")
                            time.sleep(retry_delay)
                        else:
                            raise  # 最后一次尝试失败则抛出异常
                
                logger.info(f"📄 页面标题: {page.title()}")

                # 获取文章标题
                title_elem = page.query_selector("h1")
                if title_elem:
                    article_title = title_elem.inner_text().strip()
                    logger.info(f"📰 文章标题: {article_title}")
                else:
                    article_title = "未知标题"
                    logger.warning("⚠️ 未找到文章标题")

                # 获取发布日期
                date_elem = page.query_selector("time[datetime]")
                if date_elem:
                    article_date = date_elem.get_attribute("datetime")
                    date_text = date_elem.inner_text().strip()
                    logger.info(f"📅 发布日期: {date_text} ({article_date})")
                else:
                    article_date = ""
                    date_text = ""
                    logger.warning("⚠️ 未找到发布日期")

                # 获取文章内容
                main_content = page.query_selector("[role='main']")
                content = []

                if main_content:
                    logger.info("✅ 找到主要内容容器")
                    
                    paragraphs = main_content.query_selector_all("p")
                    logger.info(f"📝 找到 {len(paragraphs)} 个段落")
                    
                    for i, ptag in enumerate(paragraphs):
                        text = ptag.inner_text().strip()
                        if text and len(text) > 20:
                            content.append(text)
                            if i < 2:  # 显示前2个段落的预览
                                logger.info(f"   段落{i+1}: {text[:100]}...")

                    logger.info(f"✅ 有效段落数: {len(content)}")

                    # 组装 Markdown
                    md = f"# {article_title}\n\n"
                    if date_text:
                        md += f"**发布日期**: {date_text}\n"
                    if article_date:
                        md += f"**日期**: {article_date}\n"
                    md += f"**原文链接**: {article_url}\n\n"
                    
                    md += "## 正文内容\n\n"
                    for p in content:
                        md += f"{p}\n\n"

                    return {
                        "title": article_title,
                        "url": article_url,
                        "date": date_text or article_date,
                        "content": content,
                        "markdown": md,
                        "success": True,
                        "error": None
                    }

                else:
                    logger.error("❌ 没有找到主要内容容器")
                    return {
                        "title": article_title,
                        "url": article_url,
                        "date": date_text or article_date,
                        "content": [],
                        "markdown": "",
                        "success": False,
                        "error": "未找到主要内容"
                    }

            except Exception as e:
                logger.error(f"❌ 抓取失败: {e}")
                return {
                    "title": "抓取失败",
                    "url": article_url,
                    "date": "",
                    "content": [],
                    "markdown": "",
                    "success": False,
                    "error": str(e)
                }
            finally:
                browser.close()

    def batch_scrape_articles(self):
        """批量抓取文章内容"""
        logger.info("📚 步骤2: 检查是否有新文章需要抓取...")
        
        # 加载文章链接
        articles = self.load_article_links()
        
        if not articles:
            logger.info("✅ 没有新文章需要抓取")
            return []
        
        logger.info(f"📰 开始批量抓取 {len(articles)} 篇新文章的内容...")
        results = []
        
        for i, article_info in enumerate(articles, 1):
            logger.info(f"🔄 抓取第 {i}/{len(articles)} 篇文章")
            
            url = article_info.get('url', '')
            if not url:
                logger.warning("⏭️ 跳过：文章链接为空")
                continue
            
            # 抓取文章内容
            result = self.scrape_article_content(url)
            
            # 补充原始信息
            result['original_info'] = article_info
            results.append(result)
            
            logger.info(f"✅ 抓取完成: {result['title'][:50]}...")
            logger.info(f"   成功: {'是' if result['success'] else '否'}")
            if result['success']:
                logger.info(f"   内容段落数: {len(result['content'])}")
            else:
                logger.info(f"   错误: {result['error']}")
            
            # 添加延迟，避免请求过快
            if i < len(articles):
                logger.info("⏰ 等待3秒后继续...")
                time.sleep(3)
        
        return results

    def create_zip_file(self, markdown_dir, zip_path):
        """创建ZIP文件"""
        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for root, dirs, files in os.walk(markdown_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        # 计算相对路径
                        arcname = os.path.relpath(file_path, markdown_dir)
                        zip_file.write(file_path, arcname)
                        logger.info(f"   添加到ZIP: {arcname}")
            logger.info(f"✅ ZIP文件创建成功: {zip_path}")
            return True
        except Exception as e:
            logger.error(f"❌ 创建ZIP文件失败: {e}")
            return False

    def save_results_for_n8n(self, results):
        """保存抓取结果到N8N监控的文件夹"""
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        logger.info("💾 步骤3: 保存文件到N8N监控目录...")
        
        # 1. 保存详细的JSON结果到N8N监控文件夹
        output_data = {
            "extraction_time": datetime.now().isoformat(),
            "total_articles": len(results),
            "successful_articles": len([r for r in results if r['success']]),
            "failed_articles": len([r for r in results if not r['success']]),
            "articles": results,
            "n8n_metadata": {
                "trigger_type": "file_created",
                "expected_actions": ["upload_to_google_drive"],
                "file_type": "json",
                "target_folder": "JSON_Data"
            }
        }
        
        json_filename = f"mckinsey_articles_content_{timestamp}.json"
        json_filepath = os.path.join(self.n8n_output_dir, json_filename)
        
        with open(json_filepath, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"✅ JSON文件已保存到N8N监控目录: {json_filepath}")
        
        # 2. 为每篇成功的文章保存单独的Markdown文件（临时目录）
        temp_markdown_dir = os.path.join(self.work_dir, f"temp_mckinsey_articles_{timestamp}")
        os.makedirs(temp_markdown_dir, exist_ok=True)
        
        successful_count = 0
        for i, result in enumerate(results, 1):
            if result['success'] and result['markdown']:
                # 生成安全的文件名
                safe_title = "".join(c for c in result['title'] if c.isalnum() or c in (' ', '-', '_')).strip()
                safe_title = safe_title[:50]  # 限制长度
                
                md_filename = f"{i:02d}_{safe_title}.md"
                md_filepath = os.path.join(temp_markdown_dir, md_filename)
                
                with open(md_filepath, "w", encoding="utf-8") as f:
                    f.write(result['markdown'])
                
                successful_count += 1
                logger.info(f"📄 Markdown文件 {i}: {md_filename}")
        
        logger.info(f"✅ 成功创建 {successful_count} 个Markdown文件")
        
        # 3. 将Markdown文件夹打包成ZIP并放到N8N监控目录
        zip_created = False
        if successful_count > 0:
            zip_filename = f"mckinsey_articles_{timestamp}.zip"
            zip_filepath = os.path.join(self.n8n_output_dir, zip_filename)
            
            logger.info(f"📦 正在创建ZIP文件: {zip_filename}")
            if self.create_zip_file(temp_markdown_dir, zip_filepath):
                logger.info(f"✅ ZIP文件已保存到N8N监控目录: {zip_filepath}")
                zip_created = True
            
            # 清理临时Markdown目录
            try:
                import shutil
                shutil.rmtree(temp_markdown_dir)
                logger.info(f"🧹 临时目录已清理: {temp_markdown_dir}")
            except Exception as e:
                logger.warning(f"⚠️ 清理临时目录失败: {e}")
        
        # 4. 创建N8N处理信息文件
        n8n_info = {
            "processing_time": datetime.now().isoformat(),
            "files_created": [json_filename],
            "zip_created": [],
            "total_files": 1,
            "instructions": {
                "json_file": "Upload to Google Drive JSON_Data folder"
            }
        }
        
        if zip_created:
            n8n_info["files_created"].append(f"mckinsey_articles_{timestamp}.zip")
            n8n_info["zip_created"].append(f"mckinsey_articles_{timestamp}.zip")
            n8n_info["total_files"] = 2
            n8n_info["instructions"]["zip_file"] = "Upload to Google Drive Markdown_Archives folder"
        
        info_filepath = os.path.join(self.n8n_output_dir, f"n8n_processing_info_{timestamp}.txt")
        with open(info_filepath, "w", encoding="utf-8") as f:
            f.write("🤖 N8N自动处理文件信息\n")
            f.write("=" * 40 + "\n\n")
            f.write(f"处理时间: {n8n_info['processing_time']}\n")
            f.write(f"创建文件数: {n8n_info['total_files']}\n\n")
            f.write("文件列表:\n")
            for file in n8n_info["files_created"]:
                f.write(f"- {file}\n")
            f.write("\n处理说明:\n")
            f.write("- JSON文件将被N8N自动上传到Google Drive的JSON_Data文件夹\n")
            if zip_created:
                f.write("- ZIP文件将被N8N自动上传到Google Drive的Markdown_Archives文件夹\n")
            f.write("- 上传完成后，本地文件可以被删除\n")
        
        logger.info(f"📋 N8N处理信息已保存: {info_filepath}")
        
        logger.info(f"🎯 文件已准备就绪，等待N8N自动处理...")
        logger.info(f"   监控目录: {self.n8n_output_dir}")
        logger.info(f"   JSON文件: {json_filename}")
        if zip_created:
            logger.info(f"   ZIP文件: mckinsey_articles_{timestamp}.zip")
        
        return n8n_info

    def run_complete_scraping(self):
        """运行完整的抓取流程"""
        logger.info("🚀 McKinsey文章完整抓取系统 - API版")
        logger.info(f"N8N监控目录: {self.n8n_output_dir}")
        
        # 步骤1: 提取最新文章链接
        new_articles = self.extract_latest_articles()
        
        # 步骤2: 批量抓取文章内容（只在有新文章时执行）
        results = self.batch_scrape_articles()
        
        # 步骤3: 保存结果并生成文件
        n8n_info = None
        if results:
            n8n_info = self.save_results_for_n8n(results)
        
        # 生成响应数据
        successful = [r for r in results if r['success']]
        failed = [r for r in results if not r['success']]
        
        response_data = {
            'success': True,
            'extraction_time': datetime.now().isoformat(),
            'new_articles_found': len(new_articles),
            'total_articles_processed': len(results),
            'successful_articles': len(successful),
            'failed_articles': len(failed),
            'articles': results,
            'n8n_info': n8n_info,
            'method': 'mckinsey_complete_scraping'
        }
        
        # 生成markdown内容
        if results:
            markdown_content = f"# McKinsey文章抓取结果\n\n"
            markdown_content += f"**抓取时间**: {response_data['extraction_time']}\n"
            markdown_content += f"**新文章数**: {len(new_articles)}\n"
            markdown_content += f"**处理总数**: {len(results)}\n"
            markdown_content += f"**成功数量**: {len(successful)}\n"
            markdown_content += f"**失败数量**: {len(failed)}\n\n"
            
            markdown_content += "## 成功抓取的文章\n\n"
            for i, article in enumerate(successful, 1):
                markdown_content += f"### {i}. {article['title']}\n\n"
                markdown_content += f"**URL**: {article['url']}\n"
                markdown_content += f"**日期**: {article.get('date', '未知')}\n"
                markdown_content += f"**段落数**: {len(article.get('content', []))}\n\n"
                # 添加前两段内容预览
                if article.get('content'):
                    markdown_content += "**内容预览**:\n"
                    for j, para in enumerate(article['content'][:2]):
                        markdown_content += f"{para}\n\n"
                    if len(article['content']) > 2:
                        markdown_content += f"... (还有 {len(article['content'])-2} 段)\n\n"
                markdown_content += "---\n\n"
            
            if failed:
                markdown_content += "## 失败的文章\n\n"
                for i, article in enumerate(failed, 1):
                    markdown_content += f"{i}. {article.get('title', '未知标题')} - {article.get('error', '未知错误')}\n"
            
            response_data['markdown'] = markdown_content
        else:
            response_data['markdown'] = "# McKinsey文章抓取结果\n\n没有新文章需要处理。"
        
        return response_data

# 创建全局的爬虫实例
scraper = McKinseyScraperAPI()

@app.route('/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'service': 'mckinsey-article-scraper-api'
    })

@app.route('/scrape', methods=['GET', 'POST'])
def scrape_articles():
    """抓取McKinsey文章接口"""
    try:
        # 获取请求参数 - 兼容GET和POST请求
        if request.method == 'POST':
            try:
                data = request.get_json(force=True) or {}
            except Exception:
                # 如果没有JSON数据或Content-Type不正确，使用空字典
                data = {}
        else:
            data = {}  # GET请求不需要参数
        
        logger.info("🚀 收到McKinsey文章抓取请求")
        
        # 清理旧文件
        scraper.clean_old_files()
        
        # 运行完整抓取流程
        result = scraper.run_complete_scraping()
        
        logger.info(f"✅ 抓取完成: 新文章 {result['new_articles_found']} 篇, 处理 {result['total_articles_processed']} 篇")
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"❌ 抓取失败: {e}")
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'error': str(e),
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route('/files/<path:filename>')
def serve_file(filename):
    """提供文件静态服务"""
    try:
        return send_from_directory(scraper.n8n_output_dir, filename)
    except Exception as e:
        logger.error(f"❌ 文件服务失败: {e}")
        return jsonify({'error': 'File not found'}), 404

@app.route('/list', methods=['GET'])
def list_files():
    """列出所有生成的文件"""
    try:
        files = []
        for filename in os.listdir(scraper.n8n_output_dir):
            filepath = os.path.join(scraper.n8n_output_dir, filename)
            if os.path.isfile(filepath):
                stat = os.stat(filepath)
                files.append({
                    'filename': filename,
                    'size': stat.st_size,
                    'created_time': datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    'modified_time': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'url': f"/files/{filename}"
                })
        
        # 按修改时间倒序排序
        files.sort(key=lambda x: x['modified_time'], reverse=True)
        
        return jsonify({
            'success': True,
            'count': len(files),
            'files': files
        })
        
    except Exception as e:
        logger.error(f"❌ 获取文件列表失败: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/clean', methods=['POST'])
def clean_old_files():
    """手动清理旧文件接口"""
    try:
        data = request.get_json() or {}
        max_age_hours = data.get('max_age_hours', 24)
        
        scraper.clean_old_files(max_age_hours)
        
        return jsonify({
            'success': True,
            'message': f'已清理超过 {max_age_hours} 小时的旧文件',
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"❌ 清理文件失败: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500

def run_server(host='0.0.0.0', port=8002, debug=False):
    """启动Flask服务器"""
    print("McKinsey文章抓取API服务器 - Zeabur部署版")
    print("=" * 50)
    print(f"服务地址: http://{host}:{port}")
    print("=" * 50)
    print("API接口:")
    print(f"  POST /scrape           - 抓取McKinsey文章")
    print(f"  GET  /health           - 健康检查")
    print("=" * 50)
    print("n8n调用示例:")
    print("  POST http://localhost:8002/scrape")
    print("  Body: {} (空对象即可)")
    print("=" * 50)
    
    if debug:
        app.run(host=host, port=port, debug=debug, threaded=True)
    else:
        # 在生产环境使用waitress服务器（避免greenlet依赖）
        try:
            from waitress import serve
            print("使用Waitress WSGI服务器")
            serve(app, host=host, port=port, threads=4)
        except ImportError:
            print("Waitress不可用，使用Flask内置服务器")
            app.run(host=host, port=port, debug=False, threaded=True)

if __name__ == '__main__':
    import sys
    
    # 解析命令行参数
    host = '0.0.0.0'
    port = int(os.environ.get('PORT', 8002))  # Zeabur会设置PORT环境变量
    debug = False
    
    for arg in sys.argv[1:]:
        if arg.startswith('--host='):
            host = arg.split('=', 1)[1]
        elif arg.startswith('--port='):
            port = int(arg.split('=', 1)[1])
        elif arg == '--debug':
            debug = True
        elif arg == '--help':
            print("使用方法:")
            print("  python mckinsey_api_server_simplified.py [选项]")
            print("选项:")
            print("  --host=HOST     服务器地址 (默认: 0.0.0.0)")
            print("  --port=PORT     端口号 (默认: 从环境变量PORT获取，或8002)")
            print("  --debug         启用调试模式")
            print("  --help          显示帮助信息")
            sys.exit(0)
    
    # 启动服务器
    run_server(host=host, port=port, debug=debug)
