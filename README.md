# 马来西亚演唱会雷达

每天 08:17、20:17（马来西亚时间）自动扫描，结果发布在 GitHub Pages。

## 资料来源

| 来源 | 方式 | 内容 |
|---|---|---|
| GoLive | 公开 JSON API | 场次、每轮预售/公售时间、排队时间、票价、限购、官方座位图 |
| Ticket2U | 公开 JSON API | 场次、每个票区价格与售罄状态、售票截止时间 |
| BookMyShow MY | 公开 JSON API（有 Cloudflare，偶尔被挡时沿用上次资料） | 场次、场馆、主办、限购 |
| Instagram | Meta 官方 Graph API（Business Discovery、标签搜索） | `config.json` 里的官方账号与标签的最新贴文 |
| Google News | RSS | 中、英、马来文新闻 |
| 人工线索 | 标题以 `[线索]` 开头的 GitHub Issue | 例如小红书链接 |

## 设定

- 改 IG 账号、标签、新闻关键词：编辑 `config.json`。
- IG token 只存在仓库的 Actions Secret `IG_PAGE_TOKEN`（永不过期的专页 token），不会出现在代码或网页。
- 有新演出、票价公布、座位图更新、新的 IG 线索时，会开一个 Issue 通知。

## 本地执行

只用 Python 标准库：

```
python radar/scan.py
```

没有 `IG_PAGE_TOKEN` 时会跳过 IG。
