"""测试用假模型：按请求内容扮演 Tutor、Grader、Verifier，并记录调用。"""

from __future__ import annotations

import re

from examprep_llm import LLMError, LLMRequest, ServerError, SystemMessage, UserMessage
from examprep_llm.testing import FakeTurn


class Brain:
    """按请求内容生成回复，并记录每类调用的次数。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_grader_on: set[str] = set()

    def __call__(self, req: LLMRequest) -> FakeTurn | LLMError:
        system = "\n".join(m.content for m in req.messages if isinstance(m, SystemMessage))
        user = [m for m in req.messages if isinstance(m, UserMessage)][-1].text
        if any(t.name == "submit_result" for t in req.tools):
            if "核对员" in system:
                self.calls.append("verify")
                items = re.findall(r"^(\d+)\. (.*)$", user, re.M)
                verdicts = [
                    {"index": int(i), "verdict": "unsupported" if "编造" in s else "supported"}
                    for i, s in items
                ]
                return FakeTurn(tool_calls=[("submit_result", {"sentences": verdicts})])
            if "判分员" in system:
                self.calls.append("grade")
                answer = user.split("## 学生答案")[-1]
                if any(key in answer for key in self.fail_grader_on):
                    return ServerError("grader down")
                right = "LIFO" in answer
                return FakeTurn(
                    tool_calls=[
                        (
                            "submit_result",
                            {
                                "verdict": "right" if right else "wrong",
                                "score": 1 if right else 0,
                                "error_type": "none" if right else "concept",
                                "reason": "意思一致" if right else "概念错误",
                            },
                        )
                    ]
                )
            self.calls.append("judge")
            wrong = "不知道" in user.split("## 学生回答")[-1]
            return FakeTurn(
                tool_calls=[
                    (
                        "submit_result",
                        {
                            "verdict": "wrong" if wrong else "right",
                            "score": 0 if wrong else 1,
                            "reason": "好",
                        },
                    )
                ]
            )
        task = user.split("## 本轮任务")[-1]
        if "换一种讲法" in task:
            kind, text = "reteach", "换个讲法：线性表就像排队 [[E1]]。"
        elif "检查理解" in task:
            kind, text = "check", "顺序表插入为什么要移动元素？"
        elif "提示" in task:
            kind, text = "hint", "提示：想想插入位置之后有几个元素 [[E1]]。"
        elif "讲评" in task:
            kind, text = "feedback", "讲评：参考答案已给出 [[E1]]。"
        elif "学生提问" in task:
            kind, text = "answer", "循环队列判满看 (rear + 1) % MaxSize == front [[E1]]。"
        elif "小结" in task:
            kind, text = "wrapup", "- 要点一 [[E1]]\n- 要点二 [[E2]]"
        else:
            kind, text = (
                "teach",
                "线性表是有序序列 [[E1]]。这句是编造的 [[E1]]。这里引用了不存在的编号 [[E9]]。",
            )
        self.calls.append(kind)
        return FakeTurn(text=text, chunk_size=3)
