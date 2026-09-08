"""
信用卡账单邮件解析模块 V2

设计原则：
1. 只从邮件提取：银行名 + 本期应还金额（还款日/账单日从卡包规则本地计算）
2. 三层容错：HTML去标签 → 模糊关键词匹配 → 智能金额提取
3. 遇到未登记银行的邮件，标记为待确认，由用户决定是否录入卡包

支持的银行及金额格式：
- 广发：本期账单金额，上下表格，纯数字 199.90
- 浦发：本期应还款总额，左右布局，¥ 689.07
- 招商：本期应还金额（标签可能为图片），¥ 559.65（金额为可选文本）
- 农行：本期应还款额(欠款为-)，表格，-194.86（负号=欠款，取绝对值）
- 中信：本期应还款总额，左右布局，CNY 28.57 / USD 0.00（只取CNY行）
- 平安：本期应还金额，左右布局，¥ 0.00 / $ 0.00（只取¥行）
"""

import re
import os
from html.parser import HTMLParser


# ============================================================
# 第一层：HTML → 纯文本归一化
# ============================================================

class _HTMLToText(HTMLParser):
    """将 HTML 转为纯文本，在块级标签处插入换行保留内容顺序"""

    BLOCK_TAGS = frozenset({
        'div', 'p', 'br', 'tr', 'table', 'thead', 'tbody', 'tfoot',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'hr',
        'blockquote', 'section', 'article', 'header', 'footer',
        'main', 'aside', 'figure', 'figcaption', 'details', 'summary',
    })

    def __init__(self):
        super().__init__()
        self._pieces = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()
        if tag_lower in ('style', 'script'):
            self._skip = True
            return
        if tag_lower in self.BLOCK_TAGS:
            self._pieces.append('\n')
        elif tag_lower in ('td', 'th'):
            self._pieces.append(' ')
        elif tag_lower == 'img':
            # 保留 alt 文本（招商的标签可能是图片但有 alt）
            alt = dict(attrs).get('alt', '')
            if alt:
                self._pieces.append(' ' + alt + ' ')

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if tag_lower in ('style', 'script'):
            self._skip = False
            return
        if tag_lower in self.BLOCK_TAGS:
            self._pieces.append('\n')

    def handle_data(self, data):
        if not self._skip:
            self._pieces.append(data)

    def handle_entityref(self, name):
        if self._skip:
            return
        entities = {
            'nbsp': ' ', 'lt': '<', 'gt': '>', 'amp': '&',
            'yen': '¥', 'mdash': '—', 'ndash': '—', 'rarr': '→',
            'reg': '®', 'copy': '©', 'trade': '™',
        }
        self._pieces.append(entities.get(name, ' '))

    def handle_charref(self, name):
        if self._skip:
            return
        try:
            if name.startswith(('x', 'X')):
                self._pieces.append(chr(int(name[1:], 16)))
            else:
                self._pieces.append(chr(int(name)))
        except (ValueError, OverflowError):
            self._pieces.append(' ')

    def get_text(self):
        text = ''.join(self._pieces)
        # 归一化空白：多个空格合并，多个换行合并
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n[ \t]*\n+', '\n', text)
        return text.strip()


def html_to_text(html_content):
    """HTML → 纯文本，去掉所有标签，保留内容语义顺序"""
    if not html_content:
        return ""
    # 如果内容不含 HTML 标签，直接返回
    if '<' not in html_content and '>' not in html_content:
        return html_content.strip()
    parser = _HTMLToText()
    try:
        parser.feed(html_content)
        return parser.get_text()
    except Exception:
        # 降级：粗暴去标签
        text = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'[ \t]+', ' ', text)
        return text.strip()


# ============================================================
# 第二层：模糊关键词匹配
# ============================================================

# "本期应还金额"的各种措辞变体，按匹配精度排序（精确在前）
AMOUNT_KEYWORDS = [
    r"本期应还款额",
    r"本期应还金额",
    r"本期账单金额",
    r"本期应还款总额",
    r"本期还款总额",
    r"本期应还总额",
    r"应还款额",
    r"应还金额",
    r"应还总额",
    r"账单金额",
    r"还款总额",
    # 英文（外资银行）
    r"[Nn]ew\s*[Bb]alance",
    r"[Tt]otal\s*[Bb]alance",
    r"[Ss]tatement\s*[Bb]alance",
]

# 最低还款关键词（遇到时跳过，不取此区域的金额）
MIN_PAYMENT_KEYWORDS = [
    r"最低还款",
    r"最低应还",
    r"最低还款额",
    r"[Mm]in(?:imum)?\s*(?:[Pp]ayment|[Rr]epayment)",
]

# 农行特殊标记
ABC_DEBT_HINT = "欠款为-"

# "上期/还款/Previous/Payment" 等语境的关键词——出现在金额上下文里说明这是历史/已收付款
# 不是本期应还金额，必须过滤掉（招行邮件正文里 "上期还款 -222.32" 这种是经典干扰源）
PRIOR_BALANCE_KEYWORDS = [
    # 中文：上期相关
    r"上期应还",
    r"上期已还",
    r"上期还款",
    r"上期账单",
    r"上期余额",
    r"上期账户",
    r"上期调整",
    r"上期金额",
    r"上期存款",
    r"上期应还款额",
    r"上期应还金额",
    r"上期还款、退货金额",
    r"上期已还款额",
    r"溢缴款",        # 余额（正值=有钱存银行，负值=欠款）
    # 中文：还款明细（交易区）
    r"还款",
    r"银联转账还款",
    r"信用卡还款",
    r"入账金额",      # 农行交易区"支出为-"
    # 英文 Previous/Payments
    r"Previous\s*+Balance",
    r"Previous\s*+Deposit",
    r"Previous\s*+Payment",
    r"[Pp]ayments?",
    r"[Pp]revious\s*+Charge",
    r"[Rr]epayment",
]


def _find_amount_keyword_position(text):
    """
    在文本中找到"本期应还金额"类关键词的位置。
    策略：
    1. 收集所有关键词匹配，按位置排序（优先顶部 = 摘要区）
    2. 排除"最低还款"语境中的匹配（"最低"在关键词前方出现）
    3. 返回最靠前的有效匹配
    """
    # 收集"最低还款"关键词的位置
    min_kw_positions = []  # (start, end)
    for kw in MIN_PAYMENT_KEYWORDS:
        for m in re.finditer(kw, text, re.IGNORECASE):
            min_kw_positions.append((m.start(), m.end()))

    def _is_min_payment_context(match_start, match_end):
        """判断此关键词匹配是否属于'最低还款'语境"""
        for ms, me in min_kw_positions:
            # 如果"最低还款"就在此关键词前面 20 字符内（同一行/同一标签组），
            # 或者此关键词就出现在"最低还款"的文本中，则跳过
            if ms <= match_start <= me:
                # 此匹配在"最低还款"文本内部（如"本期最低还款额"包含了"还款额"）
                return True
            if ms < match_start and match_start - ms < 30:
                # "最低还款"就在前面几个字符
                return True
        return False

    # 收集所有关键词匹配，按位置排序（优先靠前 = 摘要区）
    all_matches = []
    for kw in AMOUNT_KEYWORDS:
        for m in re.finditer(kw, text, re.IGNORECASE):
            all_matches.append((m.start(), m.end(), kw))

    # 按位置排序，靠前的优先
    all_matches.sort(key=lambda x: x[0])

    # 取第一个不在"最低还款"语境中的匹配
    for start, end, kw in all_matches:
        if not _is_min_payment_context(start, end):
            return (start, end)

    return None


# ============================================================
# 第三层：智能金额提取
# ============================================================

def _parse_number(s):
    """解析数字字符串，去除逗号和空格"""
    if not s:
        return None
    s = s.strip().replace(',', '').replace(' ', '')
    if not s or s == '-' or s == '+':
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _is_interest_rate(text, start, end):
    """判断此位置附近的数字是否是利率（而非金额）"""
    context_start = max(0, start - 30)
    context_end = min(len(text), end + 30)
    context = text[context_start:context_end]
    # 如果附近有"利率"、"年化"、"%"符号，大概率是利率
    if re.search(r'利率|年化|%', context):
        return True
    return False


def _is_credit_limit(value):
    """判断此金额是否可能是信用额度（整千数 >= 10000）"""
    abs_val = abs(value)
    return abs_val >= 10000 and abs_val % 1000 == 0


def _is_prior_balance_context(text, position):
    """
    判断 position 附近的金额是否属于"上期/还款/Previous/Payment"语境。

    查看金额出现前 80 字符 + 后 30 字符：
    - 如果包含 "上期"、"还款"(但不在"最低还款"中)、"Previous"、"Payment" 等关键词
    - 且没有"本期"这个反关键词 → 认为是 prior 语境，应过滤
    """
    ctx_start = max(0, position - 80)
    ctx_end = min(len(text), position + 30)
    context = text[ctx_start:ctx_end]

    # 如果同一区域出现"本期"关键词 → 这是本期数据，不是 prior
    if re.search(r'本期', context):
        return False

    # 检查 prior 关键词
    for kw in PRIOR_BALANCE_KEYWORDS:
        if re.search(kw, context, re.IGNORECASE):
            # 排除"最低还款"语境——最低还款也是还款，但金额要保留作候选
            if "最低还款" in context or "最低应还" in context or re.search(r'Min', context, re.IGNORECASE):
                continue
            return True
    return False


def extract_amount_due(text, bank_name=""):
    """
    从纯文本中提取本期应还金额。

    策略：
    1. 找到"应还金额"关键词位置
    2. 在关键词后搜索窗口内提取所有人民币金额
    3. 过滤掉 USD/$ 金额，只保留 CNY/¥/纯数字
    4. 取绝对值最大的作为本期应还金额（自动排除最低还款额）
    5. 农行特殊处理：负号=欠款取绝对值，正数=有余额不需还

    Args:
        text: 纯文本内容
        bank_name: 银行名（用于农行特殊规则判断）

    Returns:
        float or None: 本期应还金额（0.0=无需还款，None=解析失败）
    """
    if not text:
        return None

    # 找到关键词位置
    kw_pos = _find_amount_keyword_position(text)

    # ---- 第一遍：收集所有金额（含 prior 上下文），用于"成对正负"识别 ----
    # 不在这里过滤 prior，是因为部分金额（如 ¥ -222.32）虽然是上期还款，
    # 但需要它的负值来确认 ¥ 222.32 也是 prior（同一笔还款的两种显示）。
    all_amounts = []  # [(value, position), ...]
    if kw_pos:
        search_start = max(0, kw_pos[0] - 50)
        search_end = min(len(text), kw_pos[1] + 500)
        search_text = text[search_start:search_end]
        _extract_into_candidates(search_text, text, search_start, all_amounts, kw_pos_found=True, apply_prior_filter=False)
        if not all_amounts:
            head_text = text[:2000]
            _extract_into_candidates(head_text, text, 0, all_amounts, kw_pos_found=False, apply_prior_filter=False)
    else:
        head_text = text[:2000]
        _extract_into_candidates(head_text, text, 0, all_amounts, kw_pos_found=False, apply_prior_filter=False)

    if not all_amounts:
        return None

    # ---- 成对正负金额识别（prior 过滤增强）----
    # 招行已还清场景的特征：摘要区有 ¥ 222.32（正）且交易区有 ¥ -222.32（负），
    # 这两个是同一笔上期还款的两种显示。普通银行极少同时出现正负成对（除 0.00）。
    # 找同时有 +X 和 -X 的非零绝对值，过滤掉。
    positive_set = set(round(v, 2) for v, _p in all_amounts if v > 0.01)
    negative_set = set(round(-v, 2) for v, _p in all_amounts if v < -0.01)
    paired_abs = positive_set & negative_set

    # ---- 第二遍：正式收集候选，应用全部过滤 ----
    candidates = []
    if kw_pos:
        search_start = max(0, kw_pos[0] - 50)
        search_end = min(len(text), kw_pos[1] + 500)
        search_text = text[search_start:search_end]
        _extract_into_candidates(search_text, text, search_start, candidates, kw_pos_found=True, apply_prior_filter=True, paired_abs=paired_abs)
        if not candidates:
            head_text = text[:2000]
            _extract_into_candidates(head_text, text, 0, candidates, kw_pos_found=False, apply_prior_filter=True, paired_abs=paired_abs)
    else:
        head_text = text[:2000]
        _extract_into_candidates(head_text, text, 0, candidates, kw_pos_found=False, apply_prior_filter=True, paired_abs=paired_abs)

    if not candidates:
        return None

    values = [v for v, _p in candidates]

    # ---- 农行特殊处理 ----
    if bank_name in ("农业银行", "农行"):
        # 农行规则：负号=欠款（需还款），正号=余额（不需还款）
        negatives = [v for v in values if v < 0]
        if negatives:
            # 取绝对值最大的负数（最负的那个 = 欠款最多的）
            return abs(min(negatives))
        # 全是正数或零，有余额，不需还款
        return 0.0

    # ---- 通用处理：取绝对值最大的金额 ----
    # 由于已过滤 prior/credit_limit/利率，剩下来的就是本期应还 + 最低还款
    # 本期应还 > 最低还款，所以 max(abs) 即为本期应还
    best = max(values, key=lambda v: abs(v))
    return abs(best)


def _extract_into_candidates(search_text, full_text, search_offset, candidates, kw_pos_found=True, apply_prior_filter=True, paired_abs=None):
    """
    从 search_text 中提取人民币金额（含负数），写入 candidates（带 full_text 位置）。

    Args:
        search_text: 当前搜索的子串（head_text 或关键词窗口）
        full_text: 完整文本（用于 prior 语境检查）
        search_offset: search_text 在 full_text 中的起始偏移
        kw_pos_found: 关键词是否找到（决定是否启用纯小数数字兜底）
        apply_prior_filter: True=应用 prior 过滤（最终候选用），False=不过滤（用于收集所有金额以识别成对）
        paired_abs: 已识别的"成对正负绝对值"集合（这些值也要过滤掉）
    """
    if paired_abs is None:
        paired_abs = set()

    used_positions = []

    def _add(m_start, m_end, value):
        # 位置重叠检查
        for s, e in used_positions:
            if m_start < e and m_end > s:
                return
        if _is_credit_limit(value):
            return
        # full_text 中的位置 = m_start + search_offset
        full_pos = m_start + search_offset
        if _is_interest_rate(full_text, full_pos, full_pos + (m_end - m_start)):
            return
        # prior 语境过滤
        if apply_prior_filter and _is_prior_balance_context(full_text, full_pos):
            return
        # 成对正负过滤
        if apply_prior_filter and round(abs(value), 2) in paired_abs:
            return
        candidates.append((value, full_pos))
        used_positions.append((m_start, m_end))

    # 优先级1：CNY 前缀（明确标注人民币）
    for m in re.finditer(r'CNY\s*(-?\s*[\d,]+\.?\d*)', search_text, re.IGNORECASE):
        v = _parse_number(m.group(1))
        if v is not None:
            _add(m.start(), m.end(), v)

    # 优先级2：¥ ￥ 前缀
    for m in re.finditer(r'[¥￥]\s*(-?\s*[\d,]+\.?\d*)', search_text):
        v = _parse_number(m.group(1))
        if v is not None:
            _add(m.start(), m.end(), v)

    # 优先级3：纯小数数字（仅当已找到 ¥/CNY 前缀金额 OR 关键词已找到时启用）
    if kw_pos_found or candidates:
        for m in re.finditer(r'(-?\s*[\d,]+\.\d{2})', search_text):
            v = _parse_number(m.group(1))
            if v is not None and abs(v) >= 0.01:
                _add(m.start(), m.end(), v)


def extract_card_last4(text):
    """
    尝试从邮件文本中提取卡号末四位。
    常见格式：
    - 尾号1234
    - ****1234 / ********4999
    - **** **** **** 1234
    - 625996******2011（农行）
    - 5182-12**-****-9664（中信）
    """
    patterns = [
        r'尾号\s*(\d{4})',
        r'卡号[：:]\s*\*+\s*(\d{4})',
        r'\d{4,6}\*+(\d{4})\b',                  # 625996******2011
        r'\d{4}-\d{2}\*+-\*+-(\d{4})',           # 5182-12**-****-9664
        r'\*{4,}\s*(\d{4})\b',                    # ****1234 / ********4999
        r'\*{4}\s*\*{4}\s*\*{4}\s*(\d{4})',      # **** **** **** 1234
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None


# ============================================================
# 调试工具
# ============================================================

def save_debug_text(bank_name, subject, plain_text, debug_dir="debug"):
    """将转换后的纯文本保存到 debug 目录，供排查解析问题"""
    if not plain_text:
        return
    os.makedirs(debug_dir, exist_ok=True)
    # 文件名：银行名_主题前20字符_时间戳.txt
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', bank_name)
    safe_subj = re.sub(r'[\\/:*?"<>|\s]', '_', subject[:20])
    from datetime import datetime
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filepath = os.path.join(debug_dir, f"{safe_name}_{safe_subj}_{ts}.txt")
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"银行: {bank_name}\n")
            f.write(f"主题: {subject}\n")
            f.write(f"保存时间: {ts}\n")
            f.write("=" * 60 + "\n\n")
            f.write(plain_text)
    except Exception:
        pass


# ============================================================
# 对外接口
# ============================================================

def parse_bill_email(subject, body, bank_name, sender="", email_date=None,
                     debug_dir=None):
    """
    解析单封账单邮件。

    只提取：
    1. 银行名（从参数传入）
    2. 本期应还金额
    3. 卡号末四位（尽力提取）

    还款日、账单日等全部从卡包规则本地计算，不依赖邮件。

    Args:
        subject:      邮件主题
        body:         邮件正文（HTML 或纯文本，均可）
        bank_name:    已识别的银行名
        sender:       发件人地址
        email_date:   邮件接收日期 (datetime)
        debug_dir:    调试文本保存目录（None 则不保存）

    Returns:
        {
            "bank": str,                # 银行名
            "subject": str,             # 邮件主题
            "amount_due": float|None,   # 本期应还金额（0.0=无需还款，None=解析失败）
            "card_last4": str|None,     # 卡号末四位
            "email_date": datetime|None,# 邮件日期
            "raw_text": str,            # 纯文本摘要（前500字）
            "parse_ok": bool,           # 是否成功解析金额
        }
    """
    # 合并主题和正文
    combined = subject + "\n" + body

    # HTML → 纯文本
    plain_text = html_to_text(combined)

    # 调试：保存纯文本
    if debug_dir:
        save_debug_text(bank_name, subject, plain_text, debug_dir)

    # 提取金额
    amount = extract_amount_due(plain_text, bank_name)

    # 提取卡号末四位
    card_last4 = extract_card_last4(plain_text)

    # 截取摘要
    snippet = plain_text[:500].replace('\n', ' ')

    return {
        "bank": bank_name,
        "subject": subject,
        "amount_due": amount,
        "card_last4": card_last4,
        "email_date": email_date,
        "raw_text": snippet,
        "parse_ok": amount is not None,
    }
