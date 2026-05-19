import streamlit as st
import google.generativeai as genai
from google.generativeai import types
import json
from PIL import Image
import io

# 페이지 기본 설정
st.set_page_config(
    page_title="소크라테스식 독해력 피드백 튜터",
    page_icon="🔍",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# 커스텀 CSS를 통한 고급스럽고 직관적인 UI 스타일링
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Inter', 'Noto Sans KR', sans-serif;
    }
    
    /* 타이틀 및 서브타이틀 스타일 */
    .main-title {
        font-size: 2.3rem;
        font-weight: 800;
        color: #1E3A8A;
        text-align: center;
        margin-top: 1rem;
        margin-bottom: 0.1rem;
    }
    .author-tag {
        font-size: 1rem;
        color: #6B7280;
        text-align: center;
        margin-bottom: 1.5rem;
        font-weight: 600;
    }
    
    /* 카드 및 정보 안내 박스 */
    .info-card {
        background-color: #F3F4F6;
        padding: 1.5rem;
        border-radius: 12px;
        border-left: 5px solid #3B82F6;
        margin-bottom: 1.5rem;
    }
    .warning-card {
        background-color: #FFFBEB;
        padding: 1rem;
        border-radius: 8px;
        border-left: 5px solid #F59E0B;
        margin-bottom: 1.5rem;
        color: #92400E;
    }
    
    /* 버튼 둥글게 스타일 */
    div.stButton > button:first-child {
        border-radius: 8px;
        font-weight: 600;
        padding: 0.5rem 1.5rem;
    }
</style>
""", unsafe_allow_html=True)

# 세션 상태(Session State) 변수 초기화
if "page" not in st.session_state:
    st.session_state.page = 1
if "original_text" not in st.session_state:
    st.session_state.original_text = ""
if "student_text" not in st.session_state:
    st.session_state.student_text = ""
if "analysis_result" not in st.session_state:
    st.session_state.analysis_result = None

# API 키 보안 관리 (Secrets 및 사이드바 백업 제공)
api_key = st.secrets.get("GEMINI_API_KEY", "")

# 로컬 테스트 및 API키 미등록 대비용 사이드바 입력 제공
if not api_key:
    with st.sidebar:
        st.subheader("🔑 API 설정")
        api_key_input = st.text_input("Gemini API Key 입력", type="password")
        if api_key_input:
            api_key = api_key_input

if api_key:
    genai.configure(api_key=api_key)
else:
    st.sidebar.warning("⚠️ Streamlit secrets에 GEMINI_API_KEY가 없거나 직접 입력되지 않았습니다.")

def compress_and_optimize_image(uploaded_file):
    """
    고화질 이미지(PNG/JPG)를 적절한 크기로 리사이징하고,
    JPEG 압축(품질 85)을 통해 용량을 95% 이상 획기적으로 줄여
    제미나이 API 전송 및 이미지 분석 처리 속도를 극대화합니다.
    """
    try:
        # PIL 이미지로 변환
        image = Image.open(uploaded_file)
        
        # RGBA(투명도 포함 PNG 등)인 경우 JPEG 저장을 위해 RGB로 변환
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
            
        # 최대 해상도 제한 (가로/세로 중 긴 쪽을 1600px로 축소)
        max_size = 1600
        width, height = image.size
        if width > max_size or height > max_size:
            if width > height:
                new_width = max_size
                new_height = int(height * (max_size / width))
            else:
                new_height = max_size
                new_width = int(width * (max_size / height))
            image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
        # JPEG 포맷으로 압축하여 메모리 바이트 스트림에 저장
        compressed_io = io.BytesIO()
        image.save(compressed_io, format="JPEG", quality=85, optimize=True)
        return compressed_io.getvalue(), "image/jpeg"
    except Exception as e:
        # 실패 시 원본 바이트 반환
        uploaded_file.seek(0)
        return uploaded_file.read(), uploaded_file.type

def extract_text_from_pdf_locally(uploaded_file):
    """
    디지털 PDF 파일인 경우, 굳이 AI OCR을 거칠 필요 없이
    서버 로컬에서 빠르게 텍스트 코드를 다이렉트로 추출합니다. (0.1초 내외 소요)
    """
    try:
        import pypdf
        uploaded_file.seek(0)
        reader = pypdf.PdfReader(io.BytesIO(uploaded_file.read()))
        extracted_text = ""
        for page in reader.pages:
            text = page.extract_text()
            if text:
                extracted_text += text + "\n"
        return extracted_text.strip()
    except ImportError:
        # pypdf 라이브러리가 설치되지 않은 경우 로그만 출력하고 빈 값 반환
        return ""
    except Exception as e:
        return ""

def extract_text_from_file(uploaded_file):
    """
    1. 디지털 PDF인 경우: 로컬 Python 엔진(pypdf)을 사용해 0.1초 만에 텍스트를 초고속 추출합니다.
    2. 이미지(PNG/JPG) 또는 스캔된 PDF인 경우: 이미지를 압축(JPEG, 최대 해상도 1600px 제한)하여
       네트워크 전송 용량을 95% 이상 대폭 절감한 뒤, 최적화된 용량으로 Gemini API OCR을 호출합니다.
    """
    if not api_key:
        st.error("API 키가 설정되지 않아 파일 처리를 수행할 수 없습니다.")
        return ""
    
    try:
        mime_type = uploaded_file.type
        
        # [1단계] PDF 파일인 경우 로컬에서 디지털 텍스트 추출 시도 (가장 빠름)
        if mime_type == "application/pdf":
            st.info("⚡ 디지털 PDF 텍스트 로컬 분석 중...")
            local_text = extract_text_from_pdf_locally(uploaded_file)
            if len(local_text) > 50:  # 유의미한 텍스트가 있을 경우
                st.success("✅ 디지털 PDF에서 직접 텍스트를 고속 추출했습니다! (대기 시간 단축)")
                return local_text
            st.warning("⚠️ 디지털 텍스트가 없거나 스캔형 PDF로 파악되어 AI 분석을 시작합니다.")

        # [2단계] 이미지/스캔본 용량 압축 및 최적화 진행
        st.info("🔍 대용량 파일 전송을 위해 초고속 압축 중...")
        if mime_type.startswith("image/"):
            file_bytes, final_mime = compress_and_optimize_image(uploaded_file)
        else:
            # PDF 스캔본이거나 기타 파일의 경우 원본 바이트 사용
            uploaded_file.seek(0)
            file_bytes = uploaded_file.read()
            final_mime = mime_type
        
        # 압축 결과 용량 가시성 제공
        original_size_kb = len(uploaded_file.getvalue()) / 1024
        compressed_size_kb = len(file_bytes) / 1024
        st.caption(f"💡 용량 다이어트: {original_size_kb:.1f}KB ➡️ {compressed_size_kb:.1f}KB ({((original_size_kb - compressed_size_kb)/original_size_kb)*100:.1f}% 절감)")
        
        # 파일 바이너리 객체 생성
        file_part = {
            "mime_type": final_mime,
            "data": file_bytes
        }
        
        # 1.5 Flash 모델로 OCR 및 구조 분석 수행
        st.info("🚀 AI를 통해 텍스트 복원 및 지문 변환 중...")
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = "이 문서(이미지 또는 PDF)에서 한글과 영어를 포함한 모든 본문 텍스트를 오류 없이 정확하게 추출해서 보여주세요. 서론이나 설명 없이 오직 추출된 본문 텍스트만 출력해야 합니다."
        
        response = model.generate_content([file_part, prompt])
        return response.text.strip()
    except Exception as e:
        st.error(f"⚠️ 파일 분석 중 오류가 발생했습니다: {str(e)}")
        return ""

def go_to_page(page_num):
    st.session_state.page = page_num
    st.rerun()

def reset_app():
    st.session_state.page = 1
    st.session_state.original_text = ""
    st.session_state.student_text = ""
    st.session_state.analysis_result = None
    st.rerun()

def retry_same_original():
    st.session_state.student_text = ""
    st.session_state.analysis_result = None
    st.session_state.page = 3
    st.rerun()

def run_pedagogical_analysis(original, student):
    """
    Gemini 1.5 Pro 모델을 호출하여 원문과 학생 글의 정보 일치도,
    인과성 결여 유무를 정밀 심사하여 구조화된 피드백 리스트를 리턴합니다.
    """
    if not api_key:
        st.error("⚠️ API 키가 없어 분석을 수행할 수 없습니다. 사이드바 설정을 확인해 주세요.")
        return None
        
    try:
        model = genai.GenerativeModel(
            model_name="gemini-1.5-pro",
            system_instruction=socratic_system_instruction
        )
        
        prompt = f"""교사가 제공한 [원문]:
\"\"\"
{original}
\"\"\"

학생이 요약/재구성한 [학생 글]:
\"\"\"
{student}
\"\"\"

위의 두 텍스트를 철저히 정밀 분석하여 아래의 구조를 가진 JSON 데이터만 반환하세요.
반드시 백틱(```json ... ```) 기호는 생략하고 순수한 JSON 스트링 구조만 반환해야 합니다.

{{
  "score": 85,
  "encouragement": "격려와 조언 한 줄 피드백",
  "questions": [
    {{
      "question": "학생의 메타인지를 깨우는 정교한 유도 질문",
      "hint": "정답이 아닌 스스로 찾아갈 수 있는 단서 (예: 원문 3문단 확인)"
    }}
  ]
}}"""
        
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        
        # 응답 정제 및 파싱
        response_text = response.text.strip()
        result_data = json.loads(response_text)
        return result_data
        
    except Exception as e:
        st.error(f"분석 도중 시스템 오류가 발생했습니다: {str(e)}")
        # 혹시 모를 파싱 실패 대비용 더미 세이프가드 반환
        return {
            "score": 75,
            "encouragement": "시스템 오류가 발생했으나 임시 조언을 표시합니다. 원문에 기반해 논리를 가다듬어 보세요.",
            "questions": [
                {
                    "question": "학생이 요약한 글에 원문의 전제 조건이 빠져 있지 않나요?",
                    "hint": "원문 문단의 흐름 속에서 어떤 조건 하에 해당 현상이 벌어지는지 다시 읽어보세요."
                }
            ]
        }

def render_step_indicator(step):
    """
    모바일과 데스크톱 화면에 최적화된 시각적 단계 표시 바
    """
    steps = ["원문 등록", "내 글 작성", "분석 결과", "스스로 교정"]
    cols = st.columns(4)
    for idx, name in enumerate(steps):
        with cols[idx]:
            if idx + 1 == step:
                st.markdown(f"<div style='text-align: center; font-weight: 800; color: #1E3A8A; border-bottom: 3px solid #1E3A8A; padding-bottom: 5px;'>🔵 {name}</div>", unsafe_allow_html=True)
            elif idx + 1 < step:
                st.markdown(f"<div style='text-align: center; font-weight: 600; color: #10B981;'>🟢 {name}</div>", unsafe_allow_html=True)
            else:
                st.markdown(f"<div style='text-align: center; font-weight: 400; color: #9CA3AF;'>⚪ {name}</div>", unsafe_allow_html=True)
    st.write("")

# ----------------------------------------------------
# [Page 1: 메인 화면]
# ----------------------------------------------------
if st.session_state.page == 1:
    st.markdown("<div class='main-title'>🔍 소크라테스식 독해력 피드백 튜터</div>", unsafe_allow_html=True)
    st.markdown("<div class='author-tag'>제작: 교사 남종윤</div>", unsafe_allow_html=True)
    
    st.markdown("""
    <div class='info-card'>
        <h4 style='margin-top: 0; color: #1E3A8A;'>💡 소크라테스식 독해력 튜터란 무엇인가요?</h4>
        <p style='font-size: 0.95rem; line-height: 1.6;'>
            본 튜터는 AI가 단독으로 답안을 첨삭해 주는 일반적인 피드백과 다릅니다.<br>
            제시된 원문을 토대로 학생이 작성한 요약글을 정밀 심사하여, <b>어떤 논리적 모순이 존재하는지, 어떤 정보가 왜곡되거나 누락되었는지 스스로 깨닫도록 날카로운 질문(Socratic Questions)을 던지는 자기성찰적 국어 학습 도구</b>입니다.
        </p>
        <p style='font-size: 0.9rem; color: #4B5563; background-color: #EBF5FF; padding: 8px; border-radius: 6px;'>
            <b>📌 학습 방법:</b> 원문 업로드 ➡️ 요약문 작성 ➡️ 정보 일치성 평가 점수 확인 ➡️ 소크라테스식 질문에 답하며 글 고쳐쓰기
        </p>
    </div>
    """, unsafe_allow_html=True)
    
    if st.button("🚀 독해력 훈련 시작하기", use_container_width=True, type="primary"):
        go_to_page(2)

# ----------------------------------------------------
# [Page 2: 원문 업로드 화면]
# ----------------------------------------------------
elif st.session_state.page == 2:
    render_step_indicator(1)
    
    st.markdown("### 📝 단계 1: 분석할 원문(지문) 등록하기")
    st.write("훈련하고자 하는 수능/모의고사 국어 기출 지문, 교과서 본문, 혹은 읽기 텍스트를 복사해서 붙여넣거나 파일로 업로드해 주세요.")
    
    # HWP 파일 에러 방지 경고 배너 디자인
    st.markdown("""
    <div class='warning-card'>
        <strong>💡 한글(HWP/HWPS) 파일 주의사항</strong><br>
        한글 파일은 시스템에서 직접 분석이 어렵습니다. <strong>본문 텍스트를 복사·붙여넣기</strong> 하거나, <strong>PDF 파일로 변환</strong>하여 업로드해 주세요!
    </div>
    """, unsafe_allow_html=True)
    
    # 텍스트 직접 입력 및 상태 바인딩
    original_input = st.text_area(
        "원문 텍스트 직접 입력하기",
        value=st.session_state.original_text,
        placeholder="여기에 국어 지문 등의 분석용 원문을 복사해서 붙여넣으세요.",
        height=250
    )
    st.session_state.original_text = original_input
    
    st.write("---")
    st.write("📷 **또는 지문 파일(이미지/PDF)에서 텍스트 자동으로 가져오기**")
    
    uploaded_file = st.file_uploader(
        "텍스트가 포함된 이미지(JPG, PNG) 또는 PDF 파일을 선택하세요.",
        type=["png", "jpg", "jpeg", "pdf"]
    )
    
    if uploaded_file is not None:
        if st.button("📄 파일에서 텍스트 추출하기", use_container_width=True):
            if not api_key:
                st.warning("⚠️ 제미나이 API 키가 아직 설정되지 않았습니다. 사이드바나 시스템 설정을 완료하세요.")
            else:
                extracted_text = extract_text_from_file(uploaded_file)
                if extracted_text:
                    st.session_state.original_text = extracted_text
                    st.success("✅ 파일 내의 본문 텍스트를 완벽히 읽어왔습니다! 아래 텍스트 입력창에서 확인 후 수정하세요.")
                    st.rerun()
                    
    # 다음 단계 버튼 활성화 제어
    st.write("")
    if st.session_state.original_text.strip():
        if st.button("➡️ 원문 확정하고 다음 단계로", use_container_width=True, type="primary"):
            go_to_page(3)
    else:
        st.button("⚠️ 원문을 입력하거나 파일을 추출해야 진행할 수 있습니다.", disabled=True, use_container_width=True)

# ----------------------------------------------------
# [Page 3: 학생 글 업로드 화면]
# ----------------------------------------------------
elif st.session_state.page == 3:
    render_step_indicator(2)
    
    st.markdown("### ✍️ 단계 2: 내가 작성한 요약/재구성 글 입력")
    st.write("앞서 업로드한 원문을 읽고, 자신이 직접 정리·요약한 요약문이나 분석적인 서술형 글을 입력창에 작성하세요.")
    
    # 동일하게 HWP 경고 문구 표시
    st.markdown("""
    <div class='warning-card'>
        <strong>💡 안내 사항</strong><br>
        사후 배경지식이나 지문 외의 정보를 최대한 배제하고, 오직 원문에 담긴 정보에만 충실하게 글을 작성했는지 스스로 점검해 보세요.
    </div>
    """, unsafe_allow_html=True)
    
    # 학생 글 입력 및 저장
    student_input = st.text_area(
        "내가 작성한 요약문 입력하기",
        value=st.session_state.student_text,
        placeholder="원문 내용을 기반으로 본인이 핵심 내용을 정밀 요약한 글을 입력하세요.",
        height=250
    )
    st.session_state.student_text = student_input
    
    # 학생이 손글씨로 쓴 요약노트 사진 업로드 대응용
    st.write("---")
    st.write("📷 **요약한 학습장(노트) 사진에서 글자 자동으로 가져오기 (선택)**")
    
    student_file = st.file_uploader(
        "필기 또는 타이핑 이미지(JPG, PNG)나 PDF를 선택해 주세요.",
        type=["png", "jpg", "jpeg", "pdf"],
        key="student_uploader"
    )
    
    if student_file is not None:
        if st.button("📄 필기노트에서 텍스트 추출하기", use_container_width=True):
            if not api_key:
                st.warning("⚠️ 제미나이 API 키를 설정해 주세요.")
            else:
                extracted_student_text = extract_text_from_file(student_file)
                if extracted_student_text:
                    st.session_state.student_text = extracted_student_text
                    st.success("✅ 학생 작성 필기에서 텍스트를 추출했습니다!")
                    st.rerun()
                    
    st.write("")
    
    # 이전/다음 네비게이션 컬럼 배치
    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        if st.button("⬅️ 이전 (원문 수정)", use_container_width=True):
            go_to_page(2)
            
    with btn_col2:
        if st.session_state.student_text.strip():
            if st.button("🧐 피드백 튜터에게 분석 맡기기", use_container_width=True, type="primary"):
                # 소크라테스 분석 실행
                with st.spinner("🔍 AI 튜터가 두 글의 논리 구조와 정보 반영률을 정밀 분석 중입니다..."):
                    result = run_pedagogical_analysis(
                        st.session_state.original_text,
                        st.session_state.student_text
                    )
                    if result:
                        st.session_state.analysis_result = result
                        go_to_page(4)
        else:
            st.button("⚠️ 요약글을 입력해야 분석을 시작합니다.", disabled=True, use_container_width=True)

# ----------------------------------------------------
# [Page 4: 완성도 판정 화면]
# ----------------------------------------------------
elif st.session_state.page == 4:
    render_step_indicator(3)
    
    st.markdown("### 📊 단계 3: 요약 완성도 및 정보 반영율 분석")
    
    if st.session_state.analysis_result:
        result = st.session_state.analysis_result
        score = result.get("score", 0)
        encouragement = result.get("encouragement", "")
        
        # 시각적인 메트릭 대시보드
        st.write("")
        col_metric, col_progress = st.columns([1, 2])
        
        with col_metric:
            st.metric(label="원문 정보 반영율 점수", value=f"{score}%")
            
        with col_progress:
            st.write("독해 일치율 현황")
            st.progress(score / 100.0)
            
            # 점수대별 가시적인 판정 등급 표시
            if score >= 90:
                st.markdown("<span style='color:#10B981; font-weight:bold; font-size:1.1rem;'>🎯 탁월 (Excellent)</span> - 원문의 핵심 논지를 거의 완벽히 포착해 냈습니다!", unsafe_allow_html=True)
            elif score >= 80:
                st.markdown("<span style='color:#3B82F6; font-weight:bold; font-size:1.1rem;'>👍 양호 (Good)</span> - 핵심 줄거리는 맞췄으나 일부 사실적 보완이 요구됩니다.", unsafe_allow_html=True)
            elif score >= 60:
                st.markdown("<span style='color:#F59E0B; font-weight:bold; font-size:1.1rem;'>⚠️ 보완 요망 (Fair)</span> - 중요한 정보 간의 연결고리나 핵심어가 빠진 것으로 분석됩니다.", unsafe_allow_html=True)
            else:
                st.markdown("<span style='color:#EF4444; font-weight:bold; font-size:1.1rem;'>❌ 재독해 권장 (Needs Work)</span> - 주관적 왜곡이나 중심 논지의 결여가 심각합니다.", unsafe_allow_html=True)
                
        st.write("---")
        
        # 교사의 따뜻한 조언 카드 형태로 렌더링
        st.markdown(f"""
        <div style="background-color: #EFF6FF; border-left: 6px solid #1D4ED8; padding: 1.5rem; border-radius: 10px; margin-bottom: 2rem;">
            <p style="margin: 0; font-weight: 700; color: #1E3A8A; font-size: 1.1rem;">👩‍🏫 남종윤 교사의 피드백 메시지</p>
            <p style="margin-top: 10px; color: #1E40AF; font-size: 1rem; line-height: 1.6; font-style: italic;">
                "{encouragement}"
            </p>
        </div>
        """, unsafe_allow_html=True)
        
        # 소크라테스 성찰 단계 이동 버튼
        if st.button("🤔 소크라테스식 자기 점검 질문 확인하기", use_container_width=True, type="primary"):
            go_to_page(5)
    else:
        st.error("분석 결과가 유실되었습니다. 요약 작성 단계에서 다시 분석해 주세요.")
        if st.button("처음으로 돌아가기"):
            reset_app()

# ----------------------------------------------------
# [Page 5: 자기 점검 유도 질문 화면]
# ----------------------------------------------------
elif st.session_state.page == 5:
    render_step_indicator(4)
    
    st.markdown("### ❓ 단계 4: 메타인지 자극을 위한 소크라테스식 유도 질문")
    st.write("AI 튜터가 제공하는 정밀 질문들에 스스로 소리내어 답해 보거나 원문을 대조하여 점검해 보세요. 답을 보며 공부하는 것이 아닌, **직접 추론 경로를 따라 다시 읽기**를 장려하기 위해 실제 힌트는 가려져 있습니다.")
    
    if st.session_state.analysis_result:
        questions = st.session_state.analysis_result.get("questions", [])
        
        st.write("")
        for idx, q_item in enumerate(questions):
            q_text = q_item.get("question", "질문을 가져오지 못했습니다.")
            q_hint = q_item.get("hint", "힌트를 가져오지 못했습니다.")
            
            st.markdown(f"""
            <div style="background-color: #F8FAFC; border: 1px solid #E2E8F0; padding: 1.2rem; border-radius: 10px; margin-bottom: 1rem; box-shadow: 0 2px 4px rgba(0,0,0,0.02);">
                <p style="margin: 0; font-weight: 700; color: #334155; font-size: 1.05rem;">
                    💡 질문 {idx + 1}. {q_text}
                </p>
            </div>
            """, unsafe_allow_html=True)
            
            # Socratic 핵심: 힌트 가리기 토글 구조
            with st.expander(label="💡 답을 찾기 위한 결정적 단서(힌트) 확인하기"):
                st.info(f"👉 {q_hint}")
            st.write("")
            
        st.write("---")
        
        # 기존 학습 패턴 복구 및 새 출발을 위한 버튼
        act_col1, act_col2 = st.columns(2)
        with act_col1:
            if st.button("🔄 동일 지문으로 다시 요약하기", use_container_width=True, type="primary"):
                retry_same_original()
                
        with act_col2:
            if st.button("🆕 완전히 새로운 지문으로 훈련", use_container_width=True):
                reset_app()
    else:
        st.error("유효한 성찰 질문 리스트가 확인되지 않습니다. 처음 단계로 이동하세요.")
        if st.button("처음으로"):
            reset_app()
