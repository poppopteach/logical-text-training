import streamlit as st
import google.generativeai as genai
import json
import io
import fitz  # PyMuPDF: PDF 처리용
from PIL import Image
import docx
import plotly.graph_objects as go
import time

# --- [페이지 설정] ---
st.set_page_config(
    page_title="소크라테스식 독해력 튜터",
    page_icon="🦉",
    layout="centered"
)

# --- [보안 및 API 초기화] ---
# st.secrets를 통해 백그라운드에서 안전하게 API 키를 불러옵니다.
if "GEMINI_API_KEY" not in st.secrets:
    st.error("보안 키 오류: .streamlit/secrets.toml 파일에 GEMINI_API_KEY를 설정해주세요.")
    st.stop()

genai.configure(api_key=st.secrets["GEMINI_API_KEY"])

# 가벼우면서도 성능이 뛰어나고 멀티모달이 지원되는 1.5 Flash 모델 사용
MODEL_NAME = "gemini-1.5-flash"
model = genai.GenerativeModel(MODEL_NAME)

# --- [상태 관리 초기화] ---
if 'step' not in st.session_state:
    st.session_state.step = 1
if 'original_text' not in st.session_state:
    st.session_state.original_text = ""
if 'student_text' not in st.session_state:
    st.session_state.student_text = ""
if 'analysis_result' not in st.session_state:
    st.session_state.analysis_result = None

@st.cache_data(show_spinner=False)
def extract_text_from_file(file_bytes, file_name, mime_type):
    """
    업로드된 파일에서 텍스트를 추출합니다.
    텍스트/워드 파일은 직접 파싱하고, 이미지/PDF는 Gemini 멀티모달을 통해 OCR을 수행합니다.
    """
    ext = file_name.split('.')[-1].lower()
    
    try:
        if ext == 'txt':
            return file_bytes.decode('utf-8')
        
        elif ext == 'docx':
            doc = docx.Document(io.BytesIO(file_bytes))
            return "\n".join([paragraph.text for paragraph in doc.paragraphs])
        
        elif ext in ['jpg', 'jpeg', 'png']:
            image = Image.open(io.BytesIO(file_bytes))
            if image.mode != 'RGB':
                image = image.convert('RGB')
            
            img_byte_arr = io.BytesIO()
            image.save(img_byte_arr, format='JPEG', quality=95)
            optimized_bytes = img_byte_arr.getvalue()
            
            prompt = (
                "이 이미지는 수능/모의고사 등의 독해 지문 문서입니다. 다단 편집이 되어 있을 수 있으므로 "
                "글의 흐름에 맞게 정확히 텍스트만 추출해 주세요. 어떠한 부가 설명도 하지 말고 텍스트만 반환하세요."
            )
            
            response = model.generate_content([
                prompt, 
                {"mime_type": "image/jpeg", "data": optimized_bytes}
            ])
            return response.text
        
        elif ext == 'pdf':
            pdf_document = fitz.open(stream=file_bytes, filetype="pdf")
            extracted_text = ""
            max_pages = min(len(pdf_document), 5)
            
            prompt = (
                "다음은 수능/모의고사 등의 독해 지문 PDF의 페이지 이미지입니다. 다단 편집을 고려하여 "
                "논리적 흐름에 맞게 텍스트만 추출해 주세요. 부가 설명 없이 텍스트만 반환하세요."
            )
            
            for page_num in range(max_pages):
                page = pdf_document.load_page(page_num)
                pix = page.get_pixmap(dpi=150, alpha=False) 
                img_bytes = pix.tobytes("jpeg")
                
                response = model.generate_content([
                    prompt, 
                    {"mime_type": "image/jpeg", "data": img_bytes}
                ])
                extracted_text += response.text + "\n\n"
            return extracted_text.strip()
            
    except Exception as e:
        st.error(f"AI 텍스트 추출 중 통신 오류가 발생했습니다: {str(e)}")
        return ""
    
    return ""

@st.cache_data(show_spinner=False)
def analyze_comprehension(original_text, student_text):
    """
    원문과 학생 글을 비교하여 완성도를 평가하고 질문을 생성합니다.
    """
    system_prompt = """
    당신은 수백 명의 학생을 지도해 온 최고 수준의 고등학교 국어 교사이자 소크라테스식 질문의 대가입니다.
    다음 두 가지 텍스트(원문, 학생 글)를 분석하여 아래 규칙에 맞게 반드시 JSON 형식으로만 응답하세요.

    [분석 및 평가 규칙]
    1. 외부 검색을 절대 하지 마시고, 오직 제공된 '원문'만을 기준으로 평가하세요.
    2. 원문에서 표면적 정보와 추론 가능한 핵심 정보들을 파악한 후, 학생 글이 이 중 몇 %를 논리적으로 담아냈는지 0에서 100 사이의 정수로 'completion_percentage'를 계산하세요.
    3. 계산된 'completion_percentage'를 기준으로 생성할 질문의 개수(N)를 결정하세요:
       - 90% 이상: 3개 / 80~89%: 4개 / 70~79%: 5개 / 60~69%: 6개 / 그 이하: 7개
    4. 학생이 놓친 부분이나 논리적 비약이 있는 부분을 중심으로 N개의 소크라테스식 유도 질문을 만드세요.
       - 수능 국어 비문학/문학 기출문제의 발문 스타일을 참고하세요.
       - 답을 바로 주지 말고, 스스로 깨닫게 하는 아주 작은 힌트 성격의 유도 질문이어야 합니다.
    5. 정답 및 해설 부분에는 반드시 "원문의 어떤 문장/문단에 근거하여 이러한 추론이 가능한지" 논리적 연결 고리를 명확히 설명하세요.

    [JSON 출력 형식 (엄격하게 준수)]
    {
      "completion_percentage": 85,
      "questions": [
        {
          "question": "첫 번째 소크라테스식 질문 (학생의 사고를 유도하는 질문)",
          "answer_and_explanation": "해당 질문에 대한 정답 및 원문의 명확한 근거 구절과 해설"
        }
      ]
    }
    """
    
    user_prompt = f"### 원문\n{original_text}\n\n### 학생 요약/재구성 글\n{student_text}"
    
    try:
        # JSON 출력을 강제하여 안정적인 파싱 보장
        response = model.generate_content(
            contents=[system_prompt, user_prompt],
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.3 # 분석의 일관성을 위해 낮은 temperature 설정
            )
        )
        return json.loads(response.text)
    except Exception as e:
        st.error(f"분석 중 오류가 발생했습니다: {e}")
        return None

def render_gauge_chart(score):
    color = "red" if score < 60 else "orange" if score < 80 else "green"
    fig = go.Figure(go.Indicator(
        mode = "gauge+number",
        value = score,
        domain = {'x': [0, 1], 'y': [0, 1]},
        title = {'text': "학생 글 완성도", 'font': {'size': 24}},
        gauge = {
            'axis': {'range': [0, 100], 'tickwidth': 1, 'tickcolor': "darkblue"},
            'bar': {'color': color},
            'bgcolor': "white",
            'borderwidth': 2,
            'bordercolor': "gray",
            'steps': [
                {'range': [0, 60], 'color': '#ffcccc'},
                {'range': [60, 80], 'color': '#ffe6cc'},
                {'range': [80, 100], 'color': '#ccffcc'}],
        }
    ))
    fig.update_layout(height=350, margin=dict(l=10, r=10, t=50, b=10))
    return fig

if st.session_state.step == 1:
    st.markdown("<h1 style='text-align: center; color: #2C3E50; margin-bottom: 0;'>🦉 소크라테스식 독해력 튜터</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #7F8C8D; margin-top: 5px;'>제작: 교사 남종윤</p>", unsafe_allow_html=True)
    
    st.write("---")
    st.markdown("""
    ### 📖 논리적 사고력을 길러주는 AI 독해 튜터입니다.
    1. **원문** (교재, 기출문제 등)을 업로드하거나 입력합니다.
    2. 학생이 스스로 작성한 **요약/재구성 글**을 입력합니다.
    3. AI가 학생 글의 **정보 담지량(%)**을 분석합니다.
    4. 놓친 논리를 스스로 깨달을 수 있도록 **소크라테스식 유도 질문**을 던집니다.
    """)
    st.write("---")
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        if st.button("🚀 시작하기", use_container_width=True, type="primary"):
            st.session_state.step = 2
            st.rerun()

elif st.session_state.step == 2:
    st.markdown("### 📝 1단계: 원문 업로드")
    st.caption("분석의 기준이 될 원문을 입력하거나 파일을 업로드해 주세요.")
    
    tab1, tab2 = st.tabs(["✍️ 직접 입력", "📁 파일 업로드 (자동 텍스트 추출)"])
    
    temp_original_text = ""
    
    with tab1:
        text_input = st.text_area("원문 내용을 붙여넣기 하세요:", height=250, value=st.session_state.original_text)
        if text_input:
            temp_original_text = text_input

    with tab2:
        st.info("💡 이미지(jpg, png)나 PDF를 올리시면 AI가 다단 편집까지 고려하여 텍스트를 읽어냅니다!")
        uploaded_file = st.file_uploader("파일 선택 (pdf, jpg, jpeg, png, txt, docx)", type=['pdf', 'jpg', 'jpeg', 'png', 'txt', 'docx'])
        if uploaded_file is not None:
            with st.spinner("AI가 파일에서 텍스트를 추출하고 있습니다..."):
                file_bytes = uploaded_file.read()
                extracted = extract_text_from_file(file_bytes, uploaded_file.name, uploaded_file.type)
                if extracted:
                    st.success("텍스트 추출 완료!")
                    st.text_area("추출된 원문 확인:", value=extracted, height=150, disabled=True)
                    temp_original_text = extracted

    st.write("---")
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("⬅️ 이전으로", use_container_width=True):
            st.session_state.step = 1
            st.rerun()
    with col2:
        if st.button("✅ 원문 등록 완료", use_container_width=True, type="primary"):
            if not temp_original_text.strip():
                st.warning("원문 텍스트를 입력하거나 추출에 성공한 파일을 업로드해주세요.")
            else:
                st.session_state.original_text = temp_original_text
                st.session_state.step = 3
                st.rerun()

elif st.session_state.step == 3:
    st.markdown("### 🧑‍🎓 2단계: 학생 요약/재구성 글 업로드")
    st.caption("학생이 원문을 읽고 스스로 정리한 글을 입력해 주세요.")
    
    tab1, tab2 = st.tabs(["✍️ 직접 입력", "📁 파일 업로드 (자동 텍스트 추출)"])
    
    temp_student_text = ""
    
    with tab1:
        text_input = st.text_area("학생이 작성한 글을 붙여넣기 하세요:", height=250, value=st.session_state.student_text)
        if text_input:
            temp_student_text = text_input

    with tab2:
        uploaded_file = st.file_uploader("학생 글 파일 선택 (pdf, jpg, jpeg, png, txt, docx)", type=['pdf', 'jpg', 'jpeg', 'png', 'txt', 'docx'])
        if uploaded_file is not None:
            with st.spinner("AI가 파일에서 텍스트를 추출하고 있습니다..."):
                file_bytes = uploaded_file.read()
                extracted = extract_text_from_file(file_bytes, uploaded_file.name, uploaded_file.type)
                if extracted:
                    st.success("텍스트 추출 완료!")
                    st.text_area("추출된 학생 글 확인:", value=extracted, height=150, disabled=True)
                    temp_student_text = extracted

    st.write("---")
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("⬅️ 원문 다시 등록하기", use_container_width=True):
            st.session_state.step = 2
            st.rerun()
    with col2:
        if st.button("🔍 완성도 분석하기", use_container_width=True, type="primary"):
            if not temp_student_text.strip():
                st.warning("학생 글 텍스트를 입력하거나 업로드해주세요.")
            else:
                st.session_state.student_text = temp_student_text
                with st.spinner("💡 AI 튜터가 원문과 학생 글을 논리적으로 비교·분석하고 있습니다... (약 10~20초 소요)"):
                    result = analyze_comprehension(st.session_state.original_text, st.session_state.student_text)
                    if result:
                        st.session_state.analysis_result = result
                        st.session_state.step = 4
                        st.rerun()

elif st.session_state.step == 4:
    result = st.session_state.analysis_result
    score = result.get('completion_percentage', 0)
    questions = result.get('questions', [])
    
    st.markdown("## 📊 분석 결과 리포트")
    
    # 4단계: 완성도 판정 화면 (게이지 차트)
    st.plotly_chart(render_gauge_chart(score), use_container_width=True)
    st.markdown(f"<h3 style='text-align: center;'>학생 글의 완성도는 <span style='color: #E74C3C;'>{score}%</span> 입니다.</h3>", unsafe_allow_html=True)
    
    st.write("---")
    
    # 5단계: 자기 점검을 유도하는 소크라테스식 질문 화면
    st.markdown("### 🦉 소크라테스식 유도 질문")
    st.caption("아래 질문에 먼저 스스로 답해본 뒤, [답 확인 및 해설 보기]를 눌러 논리적 근거를 확인하세요.")
    
    for i, q in enumerate(questions):
        st.markdown(f"**Q{i+1}. {q.get('question', '')}**")
        with st.expander("💡 답 확인 및 해설 보기"):
            st.markdown(f"**[튜터의 해설]**\n{q.get('answer_and_explanation', '')}")
        st.write("") # 간격 띄우기
        
    st.write("---")
    
    # 하단 네비게이션
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔄 기존 글로 다시하기 (학생 글 재등록)", use_container_width=True):
            st.session_state.analysis_result = None
            st.session_state.step = 3
            st.rerun()
    with col2:
        if st.button("✨ 새 글로 다시하기 (초기화)", use_container_width=True, type="primary"):
            st.session_state.clear()
            st.rerun()
