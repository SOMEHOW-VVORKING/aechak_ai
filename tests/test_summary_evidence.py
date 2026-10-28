"""모델이 쓴 **자유 텍스트**의 출력 게이트.

인용(``quote``)에만 걸려 있던 게이트 넷(길이 상한 / 리터럴 검증 / ``mask_heavy`` /
차단 적중)이 ``aspectLabel`` 과 ``headline`` 에는 하나도 걸려 있지 않았다. 그 둘은
자유 텍스트이고, ``aspectLabel`` 은 집계표의 ``labelSamples`` 로 들어가 백엔드가
**영구 보관**한다 -- 한 번 들어간 실명·주소가 이후 모든 요약의 집계표와 화면에 계속
재등장한다.

리터럴 검증은 라벨에 쓸 수 없다(라벨은 "비린 냄새"처럼 정규화된 명사구가 정상이고
본문에 그 글자가 없다). 그래서 **쓸 수 있는 겹을 전부 쓴다**: 짧은 길이 상한,
``mask_heavy``, 차단 적중, 그리고 마스킹 복원 검사.

**탈락이 항목을 버리지 않는다.** 라벨은 읽는 축이고 ``(코드, 극성)`` 이 세는 축이다.
더러운 라벨 때문에 항목을 버리면 반복으로 셀 언급 한 건이 사라지고, 그 손해 방향에
부정 리뷰가 있다. 그래서 라벨만 열거 기본값으로 되돌리고 셈은 남긴다.
"""

from __future__ import annotations

from app.review_summary import envelope as env
from app.review_summary import evidence
from app.schemas.summary import AspectCode, Polarity, ReviewInput


def _prepared(content: str) -> env.PreparedReview:
    batch = env.prepare([ReviewInput(reviewId=7, rating=1, content=content)], content_chars=400)
    return batch.prepared[0]


def _aspect(label: str, quote: str = "배송은 엉망") -> dict[str, object]:
    return {
        "aspectCode": AspectCode.DELIVERY.value,
        "aspectLabel": label,
        "polarity": Polarity.CON.value,
        "quote": quote,
    }


_LEAK_BODY = "사장 **아 돈만 받고 배송은 엉망. 주소는 서울 강남구 역삼동 12-3 김민수입니다"


def test_라벨이_길면_떨어진다() -> None:
    """길이 상한이 유출량의 상한이다. 라벨은 상품 속성을 가리키는 짧은 명사구다."""
    source = _prepared(_LEAK_BODY)
    verified, counts = evidence.verify_batch(
        {"reviews": [{"reviewId": 7, "aspects": [_aspect("가" * (evidence.LABEL_MAX_CHARS + 1))]}]},
        [source],
    )

    # 항목은 남고 라벨만 열거 기본값으로 되돌아간다. 셈을 버리지 않는다.
    assert [item.aspectLabel for item in verified[0].aspects] == ["배송"]
    assert "LABEL_TOO_LONG" in verified[0].drop_reasons
    assert counts.label_rejected == 1


def test_라벨에_든_연락처가_떨어진다() -> None:
    source = _prepared("배송이 늦어요. 급하면 010-1234-5678 로 연락 주세요")
    verified, counts = evidence.verify_batch(
        {"reviews": [{"reviewId": 7, "aspects": [_aspect("010-1234-5678 로 연락", quote="배송이 늦어요")]}]},
        [source],
    )

    assert [item.aspectLabel for item in verified[0].aspects] == ["배송"]
    assert "LABEL_BLOCKED" in verified[0].drop_reasons
    assert counts.label_rejected == 1


def test_라벨이_마스킹을_되돌리면_떨어진다() -> None:
    """**이 레포의 불변식이다.** 백엔드 금칙어 마스킹이 가린 구간을 요약이 복원하면 안 된다.

    원문에 ``사장 **아`` 가 있는데 모델이 ``사장 병신아`` 를 쓰면, 앞뒤 맥락은 그대로이고
    가린 글자만 채워진 모양이다. 그 모양을 코드가 알아볼 수 있다.
    """
    source = _prepared(_LEAK_BODY)
    verified, counts = evidence.verify_batch(
        {"reviews": [{"reviewId": 7, "aspects": [_aspect("사장 병신아 라는 욕설")]}]},
        [source],
    )

    assert [item.aspectLabel for item in verified[0].aspects] == ["배송"]
    assert "LABEL_RESTORES_MASK" in verified[0].drop_reasons
    assert counts.mask_restored == 1


def test_정상_라벨은_통과한다() -> None:
    """과탐으로 기울이면 읽는 축이 영구히 비어 라벨 표본이 사라진다."""
    source = _prepared("배송은 엉망이었어요. 상자도 찌그러졌습니다")
    verified, counts = evidence.verify_batch(
        {"reviews": [{"reviewId": 7, "aspects": [_aspect("배송 지연", quote="배송은 엉망")]}]},
        [source],
    )

    assert [item.aspectLabel for item in verified[0].aspects] == ["배송 지연"]
    assert counts.label_rejected == 0
    assert counts.mask_restored == 0


def test_마스킹_복원_검사가_가린_구간을_언급하지_않는_라벨을_살린다() -> None:
    """원문에 ``*`` 가 있다는 이유만으로 그 리뷰의 라벨을 전부 버리면 안 된다."""
    source = _prepared(_LEAK_BODY)
    verified, counts = evidence.verify_batch(
        {"reviews": [{"reviewId": 7, "aspects": [_aspect("배송 지연")]}]},
        [source],
    )

    assert [item.aspectLabel for item in verified[0].aspects] == ["배송 지연"]
    assert counts.mask_restored == 0


# --- 검증된 것과 미검사를 섞지 않는다 ----------------------------------------------------


def test_같은_리뷰를_두_행으로_돌려줘도_검증된_항목이_살아남는다() -> None:
    """**이 레포의 1번 불변식이다.** 모델이 정상 행 뒤에 빈 행을 붙이면 검증된 단점이 사라졌다.

    호출부(``service.py``)가 ``{entry.review_id: entry}`` 로 접어 **마지막 행만** 남긴다.
    그래서 빈 행 하나가 그 리뷰의 검증된 항목 전부를 지우고 "분석했고 단점이 없었다"가 나갔다.
    추출 툴의 ``strict`` 스키마가 ``reviewId`` 유일성을 정의하지 않으므로 스키마가 막지 못한다.
    """
    source = _prepared("배송은 엉망이었어요. 상자도 찌그러졌습니다")
    verified, _counts = evidence.verify_batch(
        {
            "reviews": [
                {"reviewId": 7, "aspects": [_aspect("배송 지연", quote="배송은 엉망")]},
                {"reviewId": 7, "aspects": []},
            ]
        },
        [source],
    )

    assert len(verified) == 1, "리뷰 하나가 엔트리 둘이 되어 호출부가 마지막 행만 남긴다"
    assert [item.aspectLabel for item in verified[0].aspects] == ["배송 지연"]
    assert verified[0].reported_anything is True


def test_중복_행의_항목이_둘_다_남는다() -> None:
    """합치는 것이지 먼저 온 것을 고르는 것이 아니다. 뒤 행의 항목도 세어야 한다."""
    source = _prepared("배송은 엉망이었어요. 냄새도 심합니다")
    verified, _counts = evidence.verify_batch(
        {
            "reviews": [
                {"reviewId": 7, "aspects": [_aspect("배송 지연", quote="배송은 엉망")]},
                {
                    "reviewId": 7,
                    "aspects": [
                        {
                            "aspectCode": AspectCode.SMELL.value,
                            "aspectLabel": "비린 냄새",
                            "polarity": Polarity.CON.value,
                            "quote": "냄새도 심합니다",
                        }
                    ],
                },
            ]
        },
        [source],
    )

    assert len(verified) == 1
    assert [item.aspectCode for item in verified[0].aspects] == [AspectCode.DELIVERY, AspectCode.SMELL]


def test_먼저_검증된_펫_맥락이_뒤_행의_빈_값에_지워지지_않는다() -> None:
    source = _prepared("우리 강아지 배송은 엉망이었어요")
    verified, _counts = evidence.verify_batch(
        {
            "reviews": [
                {
                    "reviewId": 7,
                    "aspects": [_aspect("배송 지연", quote="배송은 엉망")],
                    "petMention": {"present": True, "quote": "우리 강아지", "species": "DOG"},
                },
                {"reviewId": 7, "aspects": [], "petMention": {"present": False}},
            ]
        },
        [source],
    )

    assert verified[0].pet_context is not None
    assert verified[0].pet_context.quote == "우리 강아지"


def test_aspects_가_목록이_아니면_미검사로_떨어진다() -> None:
    """목록이 아닌 것을 **빈 유효 결과로 구제하면** 통과와 미검사가 섞인다.

    ``reported_anything=False`` 가 되어 호출부의 미검사 분기에 들어가지 않고
    ``EXTRACTED`` + 분모 포함 + "단점 언급 없음"이 나갔다. 모델이 보낸 구조가
    비정상이라는 사실이 결과 어디에도 남지 않는다.
    """
    source = _prepared("배송은 엉망이었어요")
    verified, _counts = evidence.verify_batch(
        {"reviews": [{"reviewId": 7, "aspects": "broken"}]},
        [source],
    )

    assert verified[0].aspects == []
    assert verified[0].reported_anything is True, "비정상 구조를 빈 유효 결과로 구제했다"
    assert verified[0].drop_reasons == ["ASPECTS_NOT_LIST"]


def test_aspects_키가_없어도_미검사로_떨어진다() -> None:
    source = _prepared("배송은 엉망이었어요")
    verified, _counts = evidence.verify_batch({"reviews": [{"reviewId": 7}]}, [source])

    assert verified[0].reported_anything is True
    assert verified[0].drop_reasons == ["ASPECTS_NOT_LIST"]


def test_빈_목록은_항목_없음_그대로다() -> None:
    """``[]`` 는 "봤고 항목이 없었다"가 맞는 해석이다. 그쪽은 바꾸지 않는다."""
    source = _prepared("배송은 엉망이었어요")
    verified, _counts = evidence.verify_batch(
        {"reviews": [{"reviewId": 7, "aspects": []}]},
        [source],
    )

    assert verified[0].reported_anything is False
    assert verified[0].drop_reasons == []


def test_펫_품종_라벨도_인용과_같은_게이트를_지난다() -> None:
    """인용이 검증됐다는 사실이 **같은 dict 의 다른 자유 텍스트**를 보증하지 않는다.

    ``quote`` 만 본문과 대조하고 ``breedLabel`` 은 ``isinstance(str)`` 만 봤다. 바로 위
    ``aspectLabel`` 은 ``_label_gate`` 넷을 지나는데 비대칭이었다 -- 본문에 없는 연락처가
    구조화된 추출 결과에 그대로 실렸다.
    """
    source = _prepared("우리 강아지 잘 먹어요")
    verified, counts = evidence.verify_batch(
        {
            "reviews": [
                {
                    "reviewId": 7,
                    "aspects": [],
                    "petMention": {
                        "present": True,
                        "quote": "우리 강아지",
                        "species": "DOG",
                        "breedLabel": "010-1234-5678",
                        "weightKg": 4,
                        "ageYears": 2,
                    },
                }
            ]
        },
        [source],
    )

    pet = verified[0].pet_context
    assert pet is not None, "인용은 검증됐으니 펫 맥락 자체는 남는다"
    assert pet.breedLabel is None, "본문에 없는 연락처가 품종 라벨로 통과했다"
    # 라벨이 떨어져도 검증된 나머지는 남는다 -- ``aspectLabel`` 과 같은 손해 방향이다.
    assert pet.quote == "우리 강아지"
    assert pet.species == "DOG"
    assert pet.weightKg == 4.0
    assert counts.label_rejected == 1


def test_정상_품종_라벨은_통과한다() -> None:
    """과탐으로 기울이면 유사도 정렬의 품종 축이 영구히 빈다."""
    source = _prepared("우리 강아지 잘 먹어요")
    verified, counts = evidence.verify_batch(
        {
            "reviews": [
                {
                    "reviewId": 7,
                    "aspects": [],
                    "petMention": {"present": True, "quote": "우리 강아지", "breedLabel": "포메라니안"},
                }
            ]
        },
        [source],
    )

    assert verified[0].pet_context is not None
    assert verified[0].pet_context.breedLabel == "포메라니안"
    assert counts.label_rejected == 0
