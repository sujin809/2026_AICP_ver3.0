"""재시도 온도가 실제로 벌어지는지 검사한다.

v3~v9의 모든 45일 실행이 재시도 소진으로 죽었고, 원인은 재시도 2회차 이후가
전부 같은 낮은 온도였다는 것이다. 같은 오류가 반복되면 재시도 프롬프트도 글자까지
같으므로 응답이 고정점에 갇혀 10회 예산이 실제로는 2회였다. 2026-03-04/PM에서 한
agent가 attempt 2~10에 걸쳐 응답 해시가 아홉 번 동일했다.

이 파일은 그 회귀를 잡는다. 평평한 스케줄로 되돌아가면 실패한다.
"""
import unittest

from twinmarket_kr.llm.validation import retry_temperature_schedule


class RetryTemperatureScheduleTest(unittest.TestCase):
    def test_first_attempt_temperature_is_not_changed(self) -> None:
        """1회차에 통과하는 대다수 호출의 표본 분포는 건드리지 않는다."""

        for first in (0.2, 0.3, 0.7):
            self.assertEqual(retry_temperature_schedule(first, 10)[0], first)

    def test_retries_strictly_escalate_until_ceiling(self) -> None:
        schedule = retry_temperature_schedule(0.2, 10)
        retries = schedule[1:]
        climbing = retries[: retries.index(1.0) + 1]
        self.assertEqual(
            climbing,
            sorted(set(climbing)),
            f"재시도 온도가 단조 증가하지 않는다: {schedule}",
        )
        self.assertEqual(max(schedule), 1.0)

    def test_no_two_consecutive_retries_share_a_temperature_before_ceiling(self) -> None:
        """고정점을 만드는 '연속 동일 온도'가 상한 도달 전에 나오면 안 된다."""

        schedule = retry_temperature_schedule(0.2, 10)
        for index in range(1, len(schedule) - 1):
            if schedule[index] == 1.0:
                break
            self.assertNotEqual(
                schedule[index],
                schedule[index + 1],
                f"attempt {index + 1}과 {index + 2}의 온도가 같다: {schedule}",
            )

    def test_every_llm_stage_uses_an_escalating_schedule(self) -> None:
        """어느 stage든 평평한 재시도 스케줄로 되돌아가면 잡는다."""

        import inspect

        from twinmarket_kr.community import posting, reading, thinking
        from twinmarket_kr.llm import analysis, belief, decision

        for module in (belief, analysis, decision, reading, posting, thinking):
            source = inspect.getsource(module)
            self.assertNotIn(
                "else 0.1 for a in range",
                source,
                f"{module.__name__}이 평평한 재시도 온도로 되돌아갔다",
            )
            self.assertNotIn(
                "if attempt == 1 else 0.1",
                source,
                f"{module.__name__}이 평평한 재시도 온도로 되돌아갔다",
            )
            self.assertIn(
                "retry_temperature_schedule",
                source,
                f"{module.__name__}이 공용 재시도 온도 스케줄을 쓰지 않는다",
            )


if __name__ == "__main__":
    unittest.main()
