.PHONY: rules

# 生成 agent 规则文件的软链接（clone 后执行一次）
rules:
	ln -sf CLAUDE.md AGENTS.md
	ln -sf CLAUDE.md .cursorrules
