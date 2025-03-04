import os
import torch
import logging
import requests
from tqdm import tqdm
from pytorch_pretrained_bert import BertTokenizer, BertModel

__author__ = 'Jitendra Jangid, Ariel Lubonja'


huggingface_repo = "https://huggingface.co/Ariel4/biobert-embeddings/resolve/main/"


#Create and configure logger
logging.basicConfig(filename='app.log', filemode='w',format='%(asctime)s %(message)s', level=logging.INFO)

logger = logging.getLogger(__name__)


def download_or_use_existing(model_folder_path, filename):
    if os.path.isfile(model_folder_path + filename):
        print(f"Using existing " + model_folder_path + filename)
    else:
        # Download with Progress Bar
        response = requests.get(huggingface_repo + filename, stream=True)

        print("Downloading " + filename + " from HuggingFace")

        total = int(response.headers.get('content-length', 0))
        with tqdm(total=total, unit='iB', unit_scale=True, ncols=70) as bar:
            with open(model_folder_path + filename, 'wb') as f:
                for data in response.iter_content(chunk_size=1024):
                    size = f.write(data)
                    bar.update(size)

        print("File Downloaded! It is stored in: " + model_folder_path+filename)


def setup_model(model_folder_path="models/"):
    """
    Verify if the model is already downloaded, if not download it.
    """
    pytorch_model_filename = "pytorch_model.bin"
    config_json_filename = "config.json"
    vocab_filename = "vocab.txt"

    if not os.path.exists(model_folder_path):
        os.makedirs(model_folder_path)

    download_or_use_existing(model_folder_path, pytorch_model_filename)
    download_or_use_existing(model_folder_path, config_json_filename)
    download_or_use_existing(model_folder_path, vocab_filename)

    return model_folder_path

class BiobertEmbedding(object):
    """
    Encoding from BioBERT model (BERT finetuned on PubMed articles).

    Parameters
    ----------

    model : str, default Biobert.
            pre-trained BERT model
    """

    def __init__(self, model_path=None):
        model_path = setup_model() # Folder containing pytorch_model.bin, config.json and vocab.txt
        
        self.model_path = model_path
        # 检查是否有可用的GPU
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        logger.info(f"Using device: {self.device}")

        self.tokens = ""
        self.sentence_tokens = ""
        self.tokenizer = BertTokenizer.from_pretrained(self.model_path)
        # 加载预训练模型并移至GPU(如果可用)
        self.model = BertModel.from_pretrained(self.model_path)
        self.model = self.model.to(self.device)
        logger.info("Initialization Done !!")
        


    def process_text(self, text):
        """处理输入文本"""
        marked_text = "[CLS] " + text + " [SEP]"
        tokenized_text = self.tokenizer.tokenize(marked_text)
        return tokenized_text


    def handle_oov(self, tokenized_text, word_embeddings):
        """
        Handle out-of-vocabulary words by appending the word embeddings of the subwords
        """
        embeddings = []
        tokens = []
        oov_len = 1
        for token,word_embedding in zip(tokenized_text, word_embeddings):
            if token.startswith('##'):
                token = token[2:]
                tokens[-1] += token
                oov_len += 1
                embeddings[-1] += word_embedding
            else:
                if oov_len > 1:
                    embeddings[-1] /= oov_len
                tokens.append(token)
                embeddings.append(word_embedding)
        return tokens,embeddings


    def eval_fwdprop_biobert(self, tokenized_text):
        segments_ids = [1] * len(tokenized_text)
        indexed_tokens = self.tokenizer.convert_tokens_to_ids(tokenized_text)

        # 将输入张量移至GPU(如果可用)
        tokens_tensor = torch.tensor([indexed_tokens]).to(self.device)
        segments_tensors = torch.tensor([segments_ids]).to(self.device)

        self.model.eval()
        with torch.no_grad():
            encoded_layers, _ = self.model(tokens_tensor, segments_tensors)
            
        return encoded_layers


    def word_vector(self, text, handle_oov=True, filter_extra_tokens=True):

        tokenized_text = self.process_text(text)

        encoded_layers = self.eval_fwdprop_biobert(tokenized_text)

        # Concatenate the tensors for all layers. We use `stack` here to
        # create a new dimension in the tensor.
        token_embeddings = torch.stack(encoded_layers, dim=0)
        token_embeddings = torch.squeeze(token_embeddings, dim=1)
        # Swap dimensions 0 and 1.
        token_embeddings = token_embeddings.permute(1,0,2)

        # Stores the token vectors, with shape [22 x 768]
        word_embeddings = []
        logger.info("Summing last 4 layers for each token")
        # For each token in the sentence...
        for token in token_embeddings:

            # `token` is a [12 x 768] tensor
            # Sum the vectors from the last four layers.
            sum_vec = torch.sum(token[-4:], dim=0)

            # Use `sum_vec` to represent `token`.
            word_embeddings.append(sum_vec)

        self.tokens = tokenized_text
        if filter_extra_tokens:
            # filter_spec_tokens: filter [CLS], [SEP] tokens.
            word_embeddings = word_embeddings[1:-1]
            self.tokens = tokenized_text[1:-1]

        if handle_oov:
            self.tokens, word_embeddings = self.handle_oov(self.tokens,word_embeddings)
        logger.info(self.tokens)
        logger.info("Shape of Word Embeddings = %s",str(len(word_embeddings)))
        return word_embeddings



    def sentence_vector(self,text):

        logger.info("Generating sentence embedding...")
        tokenized_text = self.process_text(text)
        self.sentence_tokens = tokenized_text

        # 准备输入
        segments_ids = [1] * len(tokenized_text)
        indexed_tokens = self.tokenizer.convert_tokens_to_ids(tokenized_text)
        
        # 将输入移动到正确的设备上
        tokens_tensor = torch.tensor([indexed_tokens], device=self.device)
        segments_tensors = torch.tensor([segments_ids], device=self.device)

        # 前向传播
        with torch.no_grad():
            encoded_layers, _ = self.model(tokens_tensor, segments_tensors)
            
        # 获取最后一层的输出并计算平均值
        token_vecs = encoded_layers[11][0]
        sentence_embedding = torch.mean(token_vecs, dim=0)
        
        logger.info(f"Shape of Sentence Embeddings = {sentence_embedding.shape}")
        return sentence_embedding.cpu().numpy()  # 返回CPU上的numpy数组


if __name__ == "__main__":

    text = "Breast cancers with HER2 amplification have a higher risk of CNS metastasis and poorer prognosis."

    biobert = BiobertEmbedding()
    word_embeddings = biobert.word_vector(text)
    sentence_embedding = biobert.sentence_vector(text)
