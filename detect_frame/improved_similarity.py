"""
Improved similarity computation module
Contains several better text similarity algorithms
"""

import re
import numpy as np
from difflib import SequenceMatcher
from rapidfuzz import fuzz, process
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import hashlib

# Optional: install for better Chinese language support
try:
    import jieba

    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False


class ImprovedSimilarityCalculator:
    """Improved similarity calculator"""

    def __init__(self, use_tfidf: bool = True, use_bert: bool = False):
        """
        Initialization

        Args:
            use_tfidf: Whether to use TF-IDF vectorized similarity
            use_bert: Whether to use BERT similarity (requires sentence-transformers)
        """
        self.use_tfidf = use_tfidf
        self.use_bert = use_bert

        if use_bert:
            try:
                from sentence_transformers import SentenceTransformer
                self.bert_model = SentenceTransformer('paraphrase-MiniLM-L6-v2')
                self.use_bert = True
            except ImportError:
                print("Warning: sentence-transformers is not installed, BERT similarity will not be used")
                self.use_bert = False

    def preprocess_readme(self, text: str) -> str:
        """
        Preprocess README text
        Remove noise such as code blocks, URLs and special characters
        """
        if not text:
            return ""

        # Remove code blocks
        text = re.sub(r'```[\s\S]*?```', '', text)
        text = re.sub(r'`[^`]*`', '', text)

        # Remove URLs
        text = re.sub(r'https?://\S+', '', text)

        # Remove HTML tags
        text = re.sub(r'<[^>]+>', '', text)

        # Remove image references
        text = re.sub(r'!\[.*?\]\(.*?\)', '', text)

        # Remove special characters, keep letters, digits and common punctuation
        text = re.sub(r'[^\w\s\u4e00-\u9fff\.\,\!\?\-]', ' ', text)

        # Normalize to lowercase
        text = text.lower()

        # Remove redundant whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        # Optional: Chinese word segmentation
        if JIEBA_AVAILABLE and re.search(r'[\u4e00-\u9fff]', text):
            words = jieba.cut(text)
            text = ' '.join(words)

        return text

    def extract_structural_features(self, text1: str, text2: str) -> float:
        """
        Extract document structure features
        """

        def get_structure(text):
            lines = text.split('\n')
            headings = [l for l in lines if l.strip().startswith('#')]
            lists = [l for l in lines if re.match(r'^[\s]*[-*+]\s', l)]
            code_blocks = len(re.findall(r'```', text)) // 2

            return {
                'headings_count': len(headings),
                'lists_count': len(lists),
                'code_blocks': code_blocks,
                'line_count': len(lines),
                'avg_line_len': sum(len(l) for l in lines) / max(len(lines), 1)
            }

        s1 = get_structure(text1)
        s2 = get_structure(text2)

        # Compute structural similarity
        structural_sim = 0
        for key in ['headings_count', 'lists_count', 'code_blocks']:
            diff = abs(s1[key] - s2[key])
            max_val = max(s1[key], s2[key])
            if max_val > 0:
                structural_sim += 1 - min(diff / max_val, 1)

        return structural_sim / 3

    def calculate_rapidfuzz_similarity(self, text1: str, text2: str) -> dict:
        """
        Compute several similarity metrics using the rapidfuzz library
        """
        if not text1 or not text2:
            return {'ratio': 0, 'partial_ratio': 0, 'token_sort_ratio': 0, 'token_set_ratio': 0}

        # Standard ratio (Levenshtein distance)
        ratio = fuzz.ratio(text1, text2) / 100.0

        # Partial ratio (allows substring matching)
        partial_ratio = fuzz.partial_ratio(text1, text2) / 100.0

        # Word-order insensitive ratio
        token_sort_ratio = fuzz.token_sort_ratio(text1, text2) / 100.0

        # Token set ratio (for when one text is a subset of the other)
        token_set_ratio = fuzz.token_set_ratio(text1, text2) / 100.0

        # QRatio (takes non-alphanumeric characters into account)
        q_ratio = fuzz.QRatio(text1, text2) / 100.0

        return {
            'ratio': ratio,
            'partial_ratio': partial_ratio,
            'token_sort_ratio': token_sort_ratio,
            'token_set_ratio': token_set_ratio,
            'q_ratio': q_ratio
        }

    def calculate_tfidf_similarity(self, text1: str, text2: str) -> float:
        """
        Compute cosine similarity using TF-IDF vectorization
        """
        if not text1 or not text2:
            return 0.0

        # Tokenize the texts (simple whitespace split, since they are already preprocessed)
        docs = [text1, text2]

        # Create the TF-IDF vectorizer
        vectorizer = TfidfVectorizer(
            max_features=1000,  # Limit the number of features
            stop_words='english',  # Remove English stop words
            ngram_range=(1, 2)  # Consider unigrams and bigrams
        )

        try:
            tfidf_matrix = vectorizer.fit_transform(docs)
            similarity = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])[0][0]
            return float(similarity)
        except:
            return 0.0

    def calculate_bert_similarity(self, text1: str, text2: str) -> float:
        """
        Compute semantic similarity with a BERT model
        """
        if not self.use_bert or not text1 or not text2:
            return 0.0

        try:
            # Truncate overly long texts (BERT usually has a 512 token limit)
            max_len = 512
            text1 = text1[:max_len * 10]  # Rough truncation
            text2 = text2[:max_len * 10]

            # Encode the texts
            embeddings = self.bert_model.encode([text1, text2])

            # Compute cosine similarity
            similarity = cosine_similarity([embeddings[0]], [embeddings[1]])[0][0]
            return float(similarity)
        except Exception as e:
            print(f"BERT similarity computation failed: {e}")
            return 0.0

    def calculate_similarity(self, text1: str, text2: str, method: str = 'hybrid') -> float:
        """
        Compute overall text similarity

        Args:
            text1: First text
            text2: Second text
            method: Computation method
                - 'simple': plain SequenceMatcher (original version)
                - 'rapidfuzz': various rapidfuzz metrics
                - 'tfidf': TF-IDF cosine similarity
                - 'hybrid': mixed method (recommended)
                - 'bert': BERT semantic similarity

        Returns:
            Similarity score (0-1)
        """
        # Preprocess the texts
        text1_processed = self.preprocess_readme(text1)
        text2_processed = self.preprocess_readme(text2)

        if not text1_processed or not text2_processed:
            return 0.0

        if method == 'simple':
            # Original method
            return SequenceMatcher(None, text1_processed, text2_processed).ratio()

        elif method == 'rapidfuzz':
            # Use the standard rapidfuzz ratio
            return fuzz.ratio(text1_processed, text2_processed) / 100.0

        elif method == 'tfidf':
            return self.calculate_tfidf_similarity(text1_processed, text2_processed)

        elif method == 'bert':
            return self.calculate_bert_similarity(text1, text2)

        elif method == 'hybrid':
            # Hybrid method: combine several algorithms
            rapidfuzz_scores = self.calculate_rapidfuzz_similarity(text1_processed, text2_processed)
            tfidf_score = self.calculate_tfidf_similarity(text1_processed, text2_processed)
            structural_score = self.extract_structural_features(text1, text2)

            # Weighted fusion
            # Relies mainly on token_sort_ratio (word-order insensitive, suits READMEs)
            weights = {
                'token_sort': 0.35,  # Word-order insensitive token matching
                'partial': 0.20,  # Partial matching
                'ratio': 0.10,  # Exact matching
                'tfidf': 0.20,  # TF-IDF semantic similarity
                'structural': 0.15  # Document structure similarity
            }

            hybrid_score = (
                    weights['token_sort'] * rapidfuzz_scores['token_sort_ratio'] +
                    weights['partial'] * rapidfuzz_scores['partial_ratio'] +
                    weights['ratio'] * rapidfuzz_scores['ratio'] +
                    weights['tfidf'] * tfidf_score +
                    weights['structural'] * structural_score
            )

            return hybrid_score

        else:
            raise ValueError(f"Unknown method: {method}")

    def calculate_similarity_with_details(self, text1: str, text2: str) -> dict:
        """
        Compute a detailed similarity report
        """
        text1_processed = self.preprocess_readme(text1)
        text2_processed = self.preprocess_readme(text2)

        if not text1_processed or not text2_processed:
            return {'final_score': 0.0, 'details': {}}

        rapidfuzz_scores = self.calculate_rapidfuzz_similarity(text1_processed, text2_processed)
        tfidf_score = self.calculate_tfidf_similarity(text1_processed, text2_processed)
        structural_score = self.extract_structural_features(text1, text2)

        # Compute the final score
        final_score = (
                0.35 * rapidfuzz_scores['token_sort_ratio'] +
                0.20 * rapidfuzz_scores['partial_ratio'] +
                0.10 * rapidfuzz_scores['ratio'] +
                0.20 * tfidf_score +
                0.15 * structural_score
        )

        return {
            'final_score': final_score,
            'details': {
                'rapidfuzz_ratio': rapidfuzz_scores['ratio'],
                'rapidfuzz_partial': rapidfuzz_scores['partial_ratio'],
                'rapidfuzz_token_sort': rapidfuzz_scores['token_sort_ratio'],
                'rapidfuzz_token_set': rapidfuzz_scores['token_set_ratio'],
                'tfidf_cosine': tfidf_score,
                'structural_similarity': structural_score
            }
        }