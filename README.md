# 公众号文章转换 Issues

## 🚀 项目链接 

[shenjuexiao/mp-issues](https://github.com/shenjuexiao/mp-issues)

## 😀 一句话介绍

填写公众号文章标题、链接，工作流提取，转化为 Markdown 格式，存储文件、发布 Issues。

## 📝 文件目录

```
mp-issues/
├── .github/
│   └── workflows/
│       └── mp-issues.yml
├── mp.md
├── mp-issues.json
├── mp-issues.log
├── mp-issues.py
├── issues.md
├── issues_json/
│   ├── {issues_id}_{mp_string}.json
│   ├── ...
│   └── {issues_id}_{mp_string}.json
├── issues_md/
│   ├── {issues_id}_{mp_string}.md
│   ├── ...
│   └── {issues_id}_{mp_string}.md
└── mp_json/
    ├── {mp_number}_{mp_string}.json
    ├── ...
    └── {mp_number}_{mp_string}.json
```

