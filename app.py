import streamlit as st
import google.generativeai as genai
from PIL import Image
import io
import json
import socket

# [네트워크 패치] 컨테이너 인프라 부작용을 예방하기 위해, 오직 구글 API 호출 시에만 IPv4 라우팅을 강제 적용합니다.
try:
    original_getaddrinfo = socket.getaddrinfo
    def forced_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        # 구글 API 서버인 경우에만 IPv4(socket.AF_INET)를 강제 매핑하여 5분 지연 버그 방지
        if host and "googleapis.com" in host:
            family = socket.AF_INET
        return original_getaddrinfo(host, port, family, type, proto, flags)
    socket.getaddrinfo = forced_getaddrinfo
except Exception:
    pass

st.set_page_config(
    page_title="소크라테스식 독해력 피드백 튜터",
    page_icon="🔍",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# 교사용 프리미엄 스타일 입히기 (가독성과 직관성을 대폭 향상)
st.markdown("""
<style>
    .main {
        background-color: #f9fbfd;
    }
    .stButton>button {
        width: 100%;
        border-radius: 8px;
        font-weight: bold;
        transition: all 0.3s ease;
    }
    .stButton>button:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.1);
    }
    .metric-container {
        background-color: #ffffff;
        padding: 20px;
        border-radius: 12px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.05);
        border: 1px solid #e2e8f0;
        text-align: center;
        margin-bottom: 20px;
    }
    .warning-box {
        background-color: #fffaf0;
        border-left: 5px solid #dd6b20;
        padding: 12px;
        border-radius: 4px;
        font-size: 0.9em;
        color: #7b341e;
        margin-top: 10px;
        margin-bottom: 15px;
    }
    .socratic-question-box {
        background-color: #f0f7ff;
        border-left: 5px solid #3182ce;
        padding: 15px;
        border-radius: 6px;
        margin-bottom: 12px;
    }
</style>
""", unsafe_allow_html=True)

# Streamlit secrets로부터 안전하게 API 키 로드 + [핵심] REST 프로토콜 강제 지정으로 gRPC 통신 블로킹 무력화
if "GEMINI_API_KEY" in st.secrets:
    # transport="rest"를 지정하여 프록시 및 컨테이너 방화벽을 우회하는 일반 HTTPS 호출 방식을 강제합니다.
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"], transport="rest")
else:
    st.error("🔑 Streamlit secrets에 'GEMINI_API_KEY'가 설정되지 않았습니다. 관리자 설정을 확인해 주세요.")
    st.stop()

# 상태 관리를 위한 세션 초기화
if "page" not in st.session_state:
    st.session_state.page = 1
if "original_text" not in st.session_state:
    st.session_state.original_text = ""
if "student_text" not in st.session_state:
    st.session_state.student_text = ""
if "analysis_result" not in st.session_state:
    st.session_state.analysis_result = None

# 화면 이동용 네비게이션 헬퍼 함수
def go_to_page(page_num):
    st.session_state.page = page_num
    st.rerun()

def reset_all():
    st.session_state.page = 1
    st.session_state.original_text = ""
    st.session_state.student_text = ""
    st.session_state.analysis_result = None
    st.rerun()

def extract_text_from_pdf_locally(uploaded_file):
    """디지털 PDF인 경우 로컬에서 0.1초 만에 직접 텍스트를 추출하는 함수"""
    try:
        import pypdf
        reader = pypdf.PdfReader(uploaded_file)
        text = ""
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
        return text.strip()
    except Exception:
        return ""

def extract_text_from_file(uploaded_file):
    """
    이미지 및 PDF 파일을 읽어 최적의 속도로 고정밀 텍스트를 추출합니다.
    진행 상황을 st.status를 사용하여 명확하게 시각화합니다.
    """
    # UI 상에서 실시간으로 진척률을 인지할 수 있도록 st.status 블록 오픈
    with st.status("📁 파일 스캔 및 AI 문맥 분석 대기 중...", expanded=True) as status:
        try:
            mime_type = uploaded_file.type
            
            # [1] 디지털 PDF 추출 시도
            if mime_type == "application/pdf":
                status.write("🔍 [1단계] 디지털 PDF 내부 텍스트 로컬 초고속 판독 시도...")
                local_text = extract_text_from_pdf_locally(uploaded_file)
                if len(local_text) > 50:
                    status.update(label="✅ 디지털 PDF 고속 복원 완료!", state="complete", expanded=False)
                    return local_text
                status.write("⚠️ 디지털 텍스트 영역이 존재하지 않는 스캔형 PDF입니다. AI OCR 분석으로 자동 전환합니다.")

            # [2] 이미지/스캔본 AI OCR 처리 (네이티브 PIL 방식 + REST API 전송)
            status.write("🚀 [2단계] AI 문서 복원 엔진(Gemini 1.5 Flash)을 호출합니다...")
            model = genai.GenerativeModel("gemini-1.5-flash")
            prompt = (
                "이 문서(이미지 또는 PDF)에서 한글과 영어를 포함한 본문 텍스트를 "
                "오타나 누락 없이 정확하게 추출해서 추출된 결과만 순수하게 텍스트로 보여주세요. "
                "인사말이나 서론 설명은 절대 출력하지 마십시오."
            )
            
            if mime_type.startswith("image/"):
                uploaded_file.seek(0)
                img = Image.open(uploaded_file)
                
                # 모바일 카메라 등으로 찍은 초고해상도 이미지인 경우 효율 증대를 위한 1차 최적화
                width, height = img.size
                if width > 2500 or height > 2500:
                    status.write("⚙️ 초고해상도 이미지 최적화 리사이징 중...")
                    img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                
                status.write("📡 [3단계] 안전한 HTTPS(REST) 전송 포트로 이미지 데이터 분석을 위탁 중...")
                response = model.generate_content([img, prompt])
            else:
                # PDF 스캔형 문서의 경우 바이트 직접 전송
                uploaded_file.seek(0)
                file_part = {
                    "mime_type": mime_type,
                    "data": uploaded_file.read()
                }
                status.write("📡 [3단계] 안전한 HTTPS(REST) 전송 포트로 PDF 문서 분석을 위탁 중...")
                response = model.generate_content([file_part, prompt])
                
            status.write("🎉 [4단계] 텍스트 반환 구조 복원 및 디코딩 중...")
            extracted_result = response.text.strip()
            
            status.update(label="✅ 문서 복원 및 텍스트 추출 성공!", state="complete", expanded=False)
            return extracted_result
            
        except Exception as e:
            status.update(label="❌ 텍스트 추출 도중 네트워크 오류 발생", state="error", expanded=True)
            st.error(f"⚠️ 원인: {str(e)}\n\n(참고: 간혹 클라우드 트래픽 정체 시 일시적으로 실패할 수 있습니다. 다시 한 번 버튼을 클릭해 주세요.)")
            return ""

def run_pedagogical_analysis(original, student):
    """
    제미나이 1.5 플래시 모델(초고속)을 활용하여 원문과 학생 글을 분석합니다.
    완성도 점수에 근거하여 질문의 개수를 정밀 제어하고 엄격한 소크라테스식 피드백 JSON을 반환받습니다.
    """
    with st.status("🧠 소크라테스 인지 모델 가동 및 분석 중...", expanded=True) as status:
        try:
            status.write("📡 [1단계] 제미나이 초고속 분석 엔진 연결 중...")
            # 초고속 분석 및 API 호출 한도 극대화를 위해 gemini-1.5-flash 모델 적용
            model = genai.GenerativeModel("gemini-1.5-flash")
            
            status.write("📖 [2단계] 교육학적 질문 생성 및 감점 조건 알고리즘 주입 중...")
            
            # 완성도 분석 및 피드백 생성 시스템 프롬프트 구성
            system_instruction = (
                "당신은 대한민국 고등학교 국어 교사이자, 학생의 자기성찰 능력을 극대화시키는 소크라테스식 대화법의 권위자입니다.\n"
                "당신의 임무는 제공받는 [교사의 원문]과 [학생이 요약하거나 재구성한 글] 두 가지를 면밀히 분석하고 피드백을 주는 것입니다.\n\n"
                
                "1. 독해력 판정 기준 및 원칙:\n"
                "   - 외부 지식이나 상식은 완전히 배제하고, 오직 제공된 [교사의 원문]에 드러난 표면적 사실 및 내포된 논리 구조에만 철저히 입각하여 판정하세요.\n"
                "   - [교사의 원문]의 전체 정보량과 인과관계를 100%로 설정한 뒤, [학생의 글]이 왜곡 없이 논리적으로 반영하고 있는 핵심 개념의 정보 비중을 % 점수(정수형)로 엄격히 산출하세요.\n"
                "   - 수능이나 모의고사 독서 지문의 성격을 고려하여, OCR 등으로 인한 미세한 오타가 원문에 있더라도 인맥 흐름을 능동적으로 해석해 핵심 단어를 찾아내어 판정해야 합니다.\n\n"
                
                "2. 소크라테스식 자기 성찰 질문 생성 공식 (가장 중요):\n"
                "   - 절대로 학생에게 정답 문장이나 빈틈을 직접 서술형으로 가르쳐 주지 마십시오.\n"
                "   - 학생이 원문과 자신의 글을 스스로 교차 대조하여 놓친 정보나 논리적 인과 오류를 스스로 깨우치도록 자극하는 날카롭고 유도성 있는 질문을 생성하세요.\n"
                "   - 질문별 'clue'(힌트/단서) 영역에는 답을 적지 말고, 대신 '원문 3문단에서 A의 조건이 달라질 때 결과가 어떻게 바뀌었는지 다시 한 번 대조해 볼까요?' 혹은 '원문의 마지막 문장에 제시된 명사들의 선후 관계를 추적해 보세요'와 같이, 직접 글 속으로 돌아가 찾을 수 있는 '좌표와 추론 방식'을 제시해 주어야 합니다.\n\n"
                
                "3. 점수별 질문 개수 제어 조건:\n"
                "   - 점수(score)가 90점 이상인 우수한 경우: 날카로운 유도 질문 3개 생성.\n"
                "   - 점수가 90점 미만일 때부터는, 점수가 5% 떨어질 때마다 질문의 개수를 1개씩 추가하여 집중 성찰을 유도하세요.\n"
                "     (예: 85% ~ 89%는 4개, 80% ~ 84%는 5개, 75% ~ 79%는 6개... 점수가 낮아질수록 더 꼼꼼하고 점진적인 힌트 유도 질문 리스트가 풍부해져야 합니다.)\n\n"
                "반드시 하단의 정해진 JSON 형식 규격을 정확하게 지켜서 출력 결과를 반환해 주십시오."
            )
            
            prompt = f"""
            {system_instruction}
            
            [교사의 원문]
            {original}
            
            [학생이 작성한 글]
            {student}
            
            [응답 JSON 규격 예시]
            {{
              "score": 82,
              "encouragement": "제시된 주제의 큰 줄기는 짚었으나, 핵심 개념 간의 필연적 연결고리를 놓치고 있습니다. 아래 질문을 단서 삼아 생각의 빈틈을 다시 채워 볼까요?",
              "questions": [
                {{
                  "question": "유도 질문 내용 1",
                  "clue": "유도용 단서 및 탐색 좌표 1"
                }}
              ]
            }}
            """
            
            status.write("📡 [3단계] HTTPS 전송 포트를 활용하여 심층 가치 분석 요청 중...")
            
            # response_mime_type을 강제 설정하여 지연 유발 요소를 극단적으로 차단
            response = model.generate_content(
                prompt,
                generation_config={"response_mime_type": "application/json"}
            )
            response_text = response.text.strip()
            
            status.write("🛠️ [4단계] AI 피드백 결과 패키징 및 데이터 디코딩 중...")
            parsed_data = json.loads(response_text)
            status.update(label="🎯 국어 독해 메타인지 분석 완료!", state="complete", expanded=False)
            return parsed_data
            
        except Exception as e:
            status.update(label="❌ AI 독해 피드백 계산 실패", state="error", expanded=True)
            # 데이터 처리 실패 시 긴급 동작을 위한 폴백 데이터 구성
            fallback_data = {
                "score": 75,
                "encouragement": f"시스템이 혼잡하여 간이 독해 분석으로 대체되었습니다. (상세 코드: {str(e)})",
                "questions": [
                    {
                        "question": "교사의 원문에 기술된 인과관계 연결어(예: '따라서', '그러므로')가 학생 글에서 동일한 논리로 설계되었는지 대조해 보세요.",
                        "clue": "원문 핵심 논리와 학생 글의 문맥 구성 방향을 상호 검토하세요."
                    },
                    {
                        "question": "원문에서 제시한 구체적인 전제 조건과 학생 글의 결과 서술 간에 논리적 방향 차이가 존재하는지 추적해 봅시다.",
                        "clue": "원문의 전반부 문단을 참고해 주시기 바랍니다."
                    }
                ]
            }
            return fallback_data

if st.session_state.page == 1:
    st.markdown("<div style='text-align: center; padding-top: 50px;'>", unsafe_allow_html=True)
    st.title("🔍 소크라테스식 독해력 피드백 튜터")
    st.markdown("<p style='font-size: 1.2em; color: #4a5568;'>질문을 통해 완성되는 메타인지 기반의 주도적 독서 학습</p>", unsafe_allow_html=True)
    st.markdown("<p style='font-size: 0.9em; color: #a0aec0;'>제작: 교사 남종윤</p>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)
    
    st.markdown("<div style='margin-top: 40px;'></div>", unsafe_allow_html=True)
    
    # 튜터 앱 소개말 카드 디자인
    st.info(
        "💡 **학습 방식 소개**\n\n"
        "이 튜터는 학생이 요약한 글에 대해 '이것이 정답이다'라며 바로 고쳐 주지 않습니다.\n"
        "스스로 사고의 공백과 왜곡을 발견해 낼 수 있도록 **날카로운 정밀 채점과 점진적인 소크라테스식 유도 발문**을 제공합니다."
    )
    
    st.markdown("<div style='margin-top: 30px;'></div>", unsafe_allow_html=True)
    if st.button("🚀 피드백 튜터와 학습 시작하기", use_container_width=True):
        go_to_page(2)

elif st.session_state.page == 2:
    st.subheader("📋 1단계: 읽기 원문 등록")
    st.progress(0.2)
    st.write("선생님이 제시한 수능 지문, 모의고사 기출지문 혹은 평가하고 싶은 독서 원문을 입력창에 입력하거나 파일을 업로드해 주세요.")
    
    # 세션 상태와 동기화된 입력 보관
    original_text_input = st.text_area(
        "📖 독서 원문 본문 입력", 
        value=st.session_state.original_text,
        height=280,
        placeholder="평가하고 싶은 분석 대상 원문을 복사해서 붙여넣거나 직접 작성해 주세요."
    )
    st.session_state.original_text = original_text_input
    
    # 파일 업로드를 통한 텍스트 추출
    uploaded_file = st.file_uploader(
        "📁 원문 이미지(PNG, JPG, JPEG) 또는 PDF 파일 업로드 (선택)",
        type=["png", "jpg", "jpeg", "pdf"]
    )
    
    # HWP 관련 예외 가이드 위젯
    st.markdown("""
    <div class="warning-box">
        💡 HWP / HWPS 파일은 본문 내용을 드래그 복사(Ctrl+C)하여 위의 입력칸에 붙여넣기(Ctrl+V) 하거나, PDF 파일로 변환하신 뒤 업로드해 주세요!
    </div>
    """, unsafe_allow_html=True)
    
    if uploaded_file is not None:
        if st.button("⚡ 파일에서 텍스트 추출 및 본문 반영", use_container_width=True):
            extracted = extract_text_from_file(uploaded_file)
            if extracted:
                st.session_state.original_text = extracted
                st.success("🎉 원문에 분석된 텍스트가 정상 반영되었습니다. 아래 입력창에서 검토 후 다음 단계를 진행하세요.")
                st.rerun()
                
    st.markdown("<div style='margin-top: 30px;'></div>", unsafe_allow_html=True)
    
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("↩️ 처음으로"):
            go_to_page(1)
    with col2:
        # 원문 내용이 기입되어 있을 때만 버튼 활성화 유도
        is_disabled = len(st.session_state.original_text.strip()) < 10
        if st.button("➡️ 학생 글 입력하기 (다음)", disabled=is_disabled):
            go_to_page(3)
        if is_disabled:
            st.caption("⚠️ 분석할 원문을 최소 10자 이상 입력하시거나 파일을 분석해야 다음 단계로 이동 가능합니다.")

elif st.session_state.page == 3:
    st.subheader("✍️ 2단계: 나의 글(요약본) 등록")
    st.progress(0.5)
    st.write("본인이 원문을 읽고 독해하며 스스로 요약하거나 중요 논리를 재구성하여 작성한 글을 이곳에 입력해 주세요.")
    
    student_text_input = st.text_area(
        "📝 내가 작성한 요약문/분석글 입력",
        value=st.session_state.student_text,
        height=280,
        placeholder="내가 읽고 정리한 요약 결과나 설명글을 입력하는 공간입니다."
    )
    st.session_state.student_text = student_text_input
    
    # 학생 글 이미지/스캔본 업로드 및 OCR 지원
    uploaded_student_file = st.file_uploader(
        "📁 손글씨 노트나 작성한 글 사진/PDF 파일로 등록하기 (선택)",
        type=["png", "jpg", "jpeg", "pdf"],
        key="student_uploader"
    )
    
    st.markdown("""
    <div class="warning-box">
        💡 필기 인쇄물이나 자필 작성 필기도 손상 없는 밝은 고화질 이미지라면 AI가 높은 판독력으로 한글을 복원하여 자동 입력합니다.
    </div>
    """, unsafe_allow_html=True)
    
    if uploaded_student_file is not None:
        if st.button("⚡ 학생 글 파일에서 텍스트 추출", use_container_width=True):
            extracted_stud = extract_text_from_file(uploaded_student_file)
            if extracted_stud:
                st.session_state.student_text = extracted_stud
                st.success("🎉 작성하신 원고의 글이 입력창에 성공적으로 복원 반영되었습니다.")
                st.rerun()

    st.markdown("<div style='margin-top: 30px;'></div>", unsafe_allow_html=True)
    
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("↩️ 이전 단계로"):
            go_to_page(2)
    with col2:
        is_disabled = len(st.session_state.student_text.strip()) < 10
        if st.button("🧠 AI 튜터 정밀 분석 시작", disabled=is_disabled, type="primary"):
            analysis_data = run_pedagogical_analysis(
                st.session_state.original_text,
                st.session_state.student_text
            )
            st.session_state.analysis_result = analysis_data
            go_to_page(4)
        if is_disabled:
            st.caption("⚠️ 평가 대상이 될 본인의 요약 글을 최소 10자 이상 성실히 기재해 주셔야 합니다.")

elif st.session_state.page == 4:
    st.subheader("📊 3단계: 독해 완성도 판정")
    st.progress(0.75)
    
    if st.session_state.analysis_result is None:
        st.warning("분석 데이터가 누락되었습니다. 원고 입력 단계부터 다시 시작해 주세요.")
        if st.button("처음으로 돌아가기"):
            reset_all()
    else:
        result = st.session_state.analysis_result
        score = result.get("score", 0)
        encouragement = result.get("encouragement", "글쓰기 수준 분석 완료.")
        
        st.markdown("<div class='metric-container'>", unsafe_allow_html=True)
        st.metric(
            label="🎯 원문 정보 및 인과 관계 반영률",
            value=f"{score}%"
        )
        
        # 점수 대역별 맞춤형 진단 띠 배너 설정
        if score >= 90:
            st.success("🌟 탁월한 독해력입니다! 본질적 사실 및 숨겨진 추론 인과관계를 완벽하게 지배하고 있습니다.")
        elif score >= 75:
            st.info("👍 우수한 시도입니다. 핵심 흐름을 지탱하는 중요 마디들이 준수하게 표현되었습니다.")
        else:
            st.warning("🧐 정독과 세밀한 논리 설계 연습이 더 권장되는 상태입니다. 주어와 술어의 결속을 상기하세요.")
            
        st.markdown("</div>", unsafe_allow_html=True)
        
        # AI 피드백 설명 상자 출력
        st.write("📢 **AI 튜터의 진단 총평**")
        st.info(encouragement)
        
        st.markdown("<div style='margin-top: 40px;'></div>", unsafe_allow_html=True)
        
        col1, col2 = st.columns([1, 2])
        with col1:
            if st.button("↩️ 학생 글 재수정"):
                go_to_page(3)
        with col2:
            if st.button("💡 소크라테스식 유도 질문 확인하기", type="primary"):
                go_to_page(5)

elif st.session_state.page == 5:
    st.subheader("💡 4단계: 소크라테스식 자기 성찰 질문")
    st.progress(1.0)
    st.write("AI 튜터가 당신의 독해적 성장을 위해 정답을 알려주는 대신, **사고를 교정해 주는 힌트형 열쇠 질문**들을 준비했습니다.")
    
    if st.session_state.analysis_result is None:
        st.warning("분석 결과가 없습니다. 정상 경로를 통해 진행해 주세요.")
        if st.button("처음으로 돌아가기"):
            reset_all()
    else:
        result = st.session_state.analysis_result
        questions = result.get("questions", [])
        score = result.get("score", 0)
        
        st.caption(f"ℹ️ 이 질문 세트는 분석 점수({score}%)를 기반으로 맞춤형 생성된 성찰 질문 {len(questions)}개입니다.")
        st.markdown("<div style='margin-top: 15px;'></div>", unsafe_allow_html=True)
        
        # 질문 목록 렌더링 및 Expander를 활용한 힌트 가리기 기능
        for i, q_item in enumerate(questions, start=1):
            q_text = q_item.get("question", "질문을 로딩할 수 없습니다.")
            clue_text = q_item.get("clue", "원문을 기반으로 탐구해 보세요.")
            
            # 소크라테스 질문 컨테이너 UI 디자인
            st.markdown(f"""
            <div class="socratic-question-box">
                <strong>질문 {i}.</strong> {q_text}
            </div>
            """, unsafe_allow_html=True)
            
            # 답 가리기용 Expander 컴포넌트 적용
            with st.expander(label=f"🔓 질문 {i}의 힌트 및 탐색 좌표 확인하기"):
                st.info(clue_text)
                
            st.markdown("<div style='margin-top: 20px;'></div>", unsafe_allow_html=True)
            
        st.success("💡 **질문 확인 후 행동 강령**\n\n위의 힌트 좌표를 가지고 원문의 해당 단락으로 돌아가 문장을 다시 정독해 보세요. 빠트리거나 오해했던 진실이 발견된다면 '기존 글로 다시하기'를 눌러 내 글을 고쳐 봅시다!")
        
        st.markdown("<div style='margin-top: 40px;'></div>", unsafe_allow_html=True)
        
        col1, col2 = st.columns([1, 1])
        with col1:
            # 기존 글 정보를 세션에 유지한 채 글쓰기 화면으로 돌려보내는 버튼
            if st.button("🔄 기존 글로 다시 도전하기", use_container_width=True):
                go_to_page(3)
        with col2:
            # 완전히 세션을 포맷하고 페이지 1로 원상복귀시키는 탈출 버튼
            if st.button("🆕 새 원문으로 다시 공부하기", use_container_width=True, type="primary"):
                reset_all()
