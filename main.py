import feedparser
import datetime
import time
import pytz
import requests
from jinja2 import Template
import urllib3
import random

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 1. 核心配置 ---

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

# 备选的 RSSHub 镜像列表 (扩充版)
RSSHUB_DOMAINS = [
    # "https://rsshub.app",              # 官方 (GitHub Actions 环境下首选)
    # "https://rsshub.lihaoyu.cn", 
    # "https://rsshub.feedlib.xyz",      # 镜像1
    "https://rsshub.rssforever.com",   # 镜像2
    "https://rsshub.ktachibana.party", # 镜像3 (你之前连上过这个，只是超时了)
    "https://rsshub.pseudoyu.com",     # 镜像4 (通常较快)
    "https://rsshub.mengkang.net",     # 镜像5
    "https://rss.shab.fun",            # 镜像6
]

# --- 修改后的期刊路由配置 ---
# 策略更新：
# 1. 英文期刊继续用 IEEE 路由（配合智能镜像选择）。
# 2. 中文期刊从 CNKI 切换到 万方 (Wanfang)，以解决 503 封锁问题。

JOURNAL_PATHS = [
    # === 英文顶刊 (IEEE) ===
    {"name": "IEEE TPWRS", "path": "/ieee/journal/59/recent"},
    {"name": "IEEE TSG", "path": "/ieee/journal/5165411/recent"},
    {"name": "IEEE TSTE", "path": "/ieee/journal/5165391/recent"},
    
    # === 中文顶刊 (切换为万方数据源) ===
    # 路由格式: /wanfang/journal/{期刊ID}
    # ID查询方式: 在万方期刊页 URL 中可以看到，如 perio/zgdjgcxb
    
    # # 中国电机工程学报 (ID: zgdjgcxb)
    # {"name": "中国电机工程学报", "path": "/wanfang/journal/zgdjgcxb"},
    
    # # 电力系统自动化 (ID: dlxtzdh)
    # {"name": "电力系统自动化", "path": "/wanfang/journal/dlxtzdh"},
    
    # # 电网技术 (ID: dwjs)
    # {"name": "电网技术", "path": "/wanfang/journal/dwjs"},
    
    # # 高电压技术 (ID: gdyjs)
    # {"name": "高电压技术", "path": "/wanfang/journal/gdyjs"},
]

KEYWORDS = [
    "frequency", "inertia", "primary control", "agc", "load frequency control", 
    "virtual synchronous", "vsg", 
    "频率", "惯量", "一次调频", "自动发电控制", "虚拟同步", "调频"
]

# --- 2. 增强型网络请求函数 ---

def get_working_rsshub_domain():
    """
    寻找当前网络环境下可用的 RSSHub 域名
    """
    print("正在寻找可用的 RSSHub 镜像 (超时设定: 15s)...")
    for domain in RSSHUB_DOMAINS:
        try:
            # 这里只请求根路径，快速验证连通性
            start = time.time()
            requests.get(domain, headers=HEADERS, timeout=15, verify=False)
            elapsed = time.time() - start
            print(f"✅ 成功连接镜像: {domain} (耗时: {elapsed:.2f}s)")
            return domain
        except Exception as e:
            # 只打印简略错误信息，避免刷屏
            error_msg = str(e)
            if "connect" in error_msg.lower(): error_msg = "连接被拒绝"
            elif "time" in error_msg.lower(): error_msg = "连接超时"
            print(f"❌ 连接失败: {domain} -> {error_msg}")
            continue
            
    print("\n⚠️ 严重警告: 所有镜像均无法连接！")
    print("  1. 如果你在本地运行，请检查是否开启了 VPN (全局模式)。")
    print("  2. 建议直接推送到 GitHub Actions，那里网络环境更好。")
    return None

def fetch_content_with_retry(url):
    """
    带重试机制的下载器 (Max Retries = 3)
    """
    max_retries = 3
    # 增加超时时间到 30 秒！RSSHub 抓取 IEEE 往往很慢
    timeout_seconds = 30 
    
    for i in range(max_retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout_seconds, verify=False)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            print(f"  [Attempt {i+1}/{max_retries}] 下载失败 ({e})，正在重试...")
            time.sleep(3 + random.random() * 2) # 随机等待 3-5 秒
            
    print(f"  ❌ 最终失败: 无法下载 {url}")
    return None

# --- 3. 辅助函数 (保持不变) ---

def is_recent(entry, days=7):
    # 简单的时效性检查
    if hasattr(entry, 'published_parsed') and entry.published_parsed:
        try:
            pub_ts = time.mktime(entry.published_parsed)
            delta = datetime.datetime.now() - datetime.datetime.fromtimestamp(pub_ts)
            return delta.days <= days
        except: pass
    return True 

def is_relevant(title, summary):
    content = (str(title) + " " + str(summary)).lower()
    for kw in KEYWORDS:
        if kw.lower() in content: return True
    return False

# --- 4. 主流程 ---
S2_JOURNALS = [
    {"name": "中国电机工程学报", "s2_name": "Proceedings of the CSEE"},
    {"name": "电力系统自动化", "s2_name": "Automation of Electric Power Systems"},
    {"name": "电网技术", "s2_name": "Power System Technology"},
    {"name": "高电压技术", "s2_name": "High Voltage Engineering"},
]

S2_QUERY_KEYWORDS = "(frequency | inertia | agc | primary control | virtual synchronous | load frequency control)"


def fetch_from_semantic_scholar():
    """通过 API 获取中文期刊 (带 429 反爬重试机制)"""
    articles = []
    print(f"\n🚀 [API模式] 正在检索中文期刊 (Semantic Scholar)...")
    
    current_year = datetime.datetime.now().year
    api_url = "https://api.semanticscholar.org/graph/v1/paper/search"
    
    # 定义重试参数
    MAX_RETRIES = 3 
    
    for i, journal in enumerate(S2_JOURNALS):
        print(f"  [{i+1}/{len(S2_JOURNALS)}] 正在检索: {journal['name']} ...", end="", flush=True)
        
        query = f'journal:"{journal["s2_name"]}" {S2_QUERY_KEYWORDS}'
        params = {
            "query": query,
            "year": f"{current_year-1}-{current_year}",
            "fields": "title,url,abstract,publicationDate,year",
            "limit": 5,
            "sort": "publicationDate:desc"
        }
        
        # === 核心修改：重试循环 ===
        for attempt in range(MAX_RETRIES):
            try:
                # 游客模式必须慢一点！
                # 第一次请求等 3 秒，后续请求如果还是 429 会在下面 sleep 更久
                time.sleep(3) 
                
                resp = requests.get(api_url, params=params, headers=HEADERS, timeout=20)
                
                if resp.status_code == 200:
                    # --- 成功获取 ---
                    data = resp.json()
                    found_count = 0
                    if "data" in data and data["data"]:
                        for paper in data["data"]:
                            # 日期处理
                            pub_date = paper.get('publicationDate')
                            if not pub_date:
                                year = paper.get('year')
                                pub_date = str(year) if year else "Recent"
                            
                            articles.append({
                                "source": journal['name'],
                                "title": paper.get('title', 'No Title'),
                                "link": paper.get('url') or f"https://www.semanticscholar.org/paper/{paper.get('paperId', '')}",
                                "date": pub_date,
                                "summary": (paper.get('abstract') or "暂无摘要")[:200] + "..."
                            })
                            found_count += 1
                    print(f" ✅ 成功 (找到 {found_count} 篇)")
                    break # 跳出重试循环，处理下一个期刊
                
                elif resp.status_code == 429:
                    # --- 触发限流，进入冷却 ---
                    wait_time = 30 * (attempt + 1) # 第一次等30s，第二次等60s...
                    print(f"\n    ⚠️ 触发限流 (429)，休息 {wait_time} 秒后重试 ({attempt+1}/{MAX_RETRIES})...", end="", flush=True)
                    time.sleep(wait_time)
                    continue # 继续下一次循环尝试
                
                else:
                    print(f" ❌ 失败 ({resp.status_code})")
                    break # 其他错误（如404/500）通常重试没用，直接跳过
                    
            except Exception as e:
                print(f" ❌ 异常: {e}")
                break

    return articles



def main():
    base_domain = get_working_rsshub_domain()
    if not base_domain:
        return # 无法继续

    relevant_articles = []
    # 建议先设置大一点的时间窗口测试，跑通后再改回 7 天

    cn_articles = fetch_from_semantic_scholar()
    relevant_articles.extend(cn_articles)

    TIME_WINDOW_DAYS = 30 
    
    print(f"\n开始抓取... (时间窗口: {TIME_WINDOW_DAYS} 天)")
    
    for item in JOURNAL_PATHS:
        full_url = base_domain + item['path']
        print(f"正在处理: {item['name']} ...")
        
        xml_content = fetch_content_with_retry(full_url)
        if not xml_content: continue
            
        try:
            feed = feedparser.parse(xml_content)
            count = 0
            if not feed.entries:
                print(f"  -> 内容为空 (可能是源暂时无数据)")
                continue

            for entry in feed.entries:
                title = entry.get('title', 'No Title')
                summary = entry.get('summary', '')
                link = entry.get('link', '#')
                date_str = entry.get('published', '') or entry.get('updated', '')
                
                if is_recent(entry, days=TIME_WINDOW_DAYS):
                    if is_relevant(title, summary):
                        relevant_articles.append({
                            "source": item['name'],
                            "title": title,
                            "link": link,
                            "date": date_str,
                            "summary": summary[:200] + "..." if len(summary) > 200 else summary
                        })
                        count += 1
            print(f"  -> 筛选出 {count} 条相关文章")
        except Exception as e:
            print(f"  -> 解析异常: {e}")

    generate_html(relevant_articles)
    print("\n✅ 更新完成！请打开 index.html 查看。")

def generate_html(articles):
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
    
    template_str = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>电网频率研究追踪</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body { 
                background-color: #f0f2f5; 
                padding: 30px 0; 
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; 
            }
            .main-container {
                max-width: 900px;
                margin: 0 auto;
                padding: 0 15px;
            }
            .header-section {
                text-align: center;
                margin-bottom: 40px;
            }
            .article-card { 
                background: #fff;
                border-radius: 12px;
                padding: 24px;
                margin-bottom: 20px; 
                border-left: 6px solid #0d6efd; 
                box-shadow: 0 2px 8px rgba(0,0,0,0.06);
                transition: transform 0.2s ease;
            }
            .article-card:hover { 
                transform: translateY(-2px); 
                box-shadow: 0 8px 16px rgba(0,0,0,0.1); 
            }
            
            /* 期刊标签样式 - 使用固定 px 大小，防止缩小 */
            .badge-source { 
                display: inline-block;
                background-color: #e7f1ff; 
                color: #0d6efd; 
                padding: 6px 12px; 
                border-radius: 6px; 
                font-size: 13px !important; /* 强制固定大小 */
                font-weight: 700; 
                text-transform: uppercase;
                letter-spacing: 0.5px;
                vertical-align: middle;
            }
            
            /* 中文期刊特殊配色 */
            .tag-cn .badge-source {
                background-color: #fce4ec;
                color: #c2185b;
            }
            .tag-cn {
                border-left-color: #d63384;
            }

            /* 日期样式 - 确保显示 */
            .article-date {
                display: inline-block;
                font-size: 13px;
                color: #6c757d;
                margin-left: 12px;
                vertical-align: middle;
                font-weight: 500;
            }

            /* 标题样式 */
            .article-title {
                display: block;
                margin-top: 12px;
                margin-bottom: 10px;
                font-size: 1.25rem;
                font-weight: 700;
                line-height: 1.5;
                color: #212529;
                text-decoration: none;
            }
            .article-title:hover {
                color: #0d6efd;
            }

            /* 摘要样式 */
            .article-summary {
                font-size: 0.95rem;
                color: #495057;
                line-height: 1.6;
                margin-bottom: 0;
            }
        </style>
    </head>
    <body>
        <div class="main-container">
            <div class="header-section">
                <h2 class="fw-bold">⚡️ 电力系统频率专题追踪</h2>
                <div class="text-muted mt-2">
                    <small>Update: {{ update_time }} | Sources: IEEE & CSEE/Automation</small>
                </div>
            </div>
            
            {% if articles %}
                {% for article in articles %}
                <!-- 根据来源判断是否添加 tag-cn 类 -->
                <div class="article-card {% if '学报' in article.source or '技术' in article.source or '自动化' in article.source %}tag-cn{% endif %}">
                    <div class="d-flex align-items-center flex-wrap">
                        <span class="badge-source">{{ article.source }}</span>
                        <!-- 强制显示日期，前面加个图标 -->
                        <span class="article-date">📅 {{ article.date }}</span>
                    </div>
                    
                    <a href="{{ article.link }}" target="_blank" class="article-title">
                        {{ article.title }}
                    </a>
                    
                    <p class="article-summary">
                        {{ article.summary }}
                    </p>
                </div>
                {% endfor %}
            {% else %}
                <div class="alert alert-secondary text-center py-5" role="alert">
                    <h5 class="alert-heading">No Updates Found</h5>
                    <p>Recent scan across Semantic Scholar (API) and RSSHub yielded no new articles matching your keywords.</p>
                </div>
            {% endif %}
            
            <footer class="text-center mt-5 text-muted small">
                Power System Frequency Tracker | Generated by Python
            </footer>
        </div>
    </body>
    </html>
    """
    
    template = Template(template_str)
    html_content = template.render(articles=articles, update_time=now)
    
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)

if __name__ == "__main__":
    main()
