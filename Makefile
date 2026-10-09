# Unix 侧的薄封装。真正的实现在 scripts/tasks.py（Windows 本地没有 make）。
# Windows 本地请直接用： uv run python scripts/tasks.py check

TASKS := uv run python scripts/tasks.py

.PHONY: setup test-unit test-int test-offline test-target test-eval lint fmt check

setup:
	$(TASKS) setup

test-unit:
	$(TASKS) test-unit

test-int:
	$(TASKS) test-int

test-offline:
	$(TASKS) test-offline

test-target:
	$(TASKS) test-target

test-eval:
	$(TASKS) test-eval

lint:
	$(TASKS) lint

fmt:
	$(TASKS) fmt

check:
	$(TASKS) check
