import torch
import torch.nn.functional as F
from torch import nn, einsum
from flash_attn import flash_attn_qkvpacked_func

from einops import rearrange, repeat

# feedforward and attention

class GEGLU(nn.Module):
    def forward(self, x):
        x, gates = x.chunk(2, dim = -1)
        return x * F.gelu(gates)

def FeedForward(dim, mult = 4, dropout = 0.):
    return nn.Sequential(
        nn.LayerNorm(dim),
        nn.Linear(dim, dim * mult * 2),
        GEGLU(),
        nn.Dropout(dropout),
        nn.Linear(dim * mult, dim)
    )

class Attention(nn.Module):
    def __init__(
        self,
        dim,
        heads = 8,
        dim_head = 64,
        dropout = 0.,
        use_flash_attn = False
    ):
        super().__init__()
        inner_dim = dim_head * heads
        self.heads = heads
        self.scale = dim_head ** -0.5

        self.norm = nn.LayerNorm(dim)

        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias = False)
        self.to_out = nn.Linear(inner_dim, dim, bias = False)

        self.dropout = nn.Dropout(dropout)
        self.use_flash_attn = use_flash_attn

    def forward(self, x, return_attn=False):
        h = self.heads

        x = self.norm(x)

        qkv = self.to_qkv(x)
        qkv = qkv.chunk(3, dim=-1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), qkv)

        if self.use_flash_attn:
            out = flash_attn_qkvpacked_func(
                torch.stack([q, k, v], dim=2),
                dropout_p=self.dropout.p,
                softmax_scale=self.scale,
                causal=False,
            )
            out = rearrange(out, 'b h n d -> b n (h d)')
        else:
            sim = einsum('b h i d, b h j d -> b h i j', q, k) * self.scale

            attn = sim.softmax(dim=-1)
            attn = self.dropout(attn)

            out = einsum('b h i j, b h j d -> b h i d', attn, v)
            out = rearrange(out, 'b h n d -> b n (h d)')

        return self.to_out(out)

# transformer

class Transformer(nn.Module):
    def __init__(
        self,
        dim,
        depth,
        heads,
        dim_head,
        attn_dropout,
        ff_dropout,
        checkpoint_grads=False,
        use_flash_attn=False
    ):
        super().__init__()
        self.layers = nn.ModuleList([])

        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                Attention(dim, heads=heads, dim_head=dim_head, dropout=attn_dropout, use_flash_attn=use_flash_attn),
                FeedForward(dim, dropout=ff_dropout),
            ]))

        self.checkpoint_grads = checkpoint_grads

    def forward(self, x, return_attn=False):
        post_softmax_attns = []

        for attn, ff in self.layers:
            if return_attn:
                attn_out, post_softmax_attn = attn(x, return_attn=True)
                post_softmax_attns.append(post_softmax_attn)
            else:
                attn_out = attn(x, return_attn=False)

            if self.checkpoint_grads:
                x = torch.utils.checkpoint.checkpoint(lambda: attn_out + x)
                x = torch.utils.checkpoint.checkpoint(ff, x) + x
            else:
                x = attn_out + x
                x = ff(x) + x

        if not return_attn:
            return x

        return x, torch.stack(post_softmax_attns)

# numerical embedder

class NumericalEmbedder(nn.Module):
    def __init__(self, dim, num_numerical_types):
        super().__init__()
        self.weights = nn.Parameter(torch.randn(num_numerical_types, dim))
        self.biases = nn.Parameter(torch.randn(num_numerical_types, dim))

    def forward(self, x):
        x = rearrange(x, 'b n -> b n 1')
        return x * self.weights + self.biases

# main class

class FTTransformer(nn.Module):
    def __init__(
        self,
        *,
        categories,
        num_continuous,
        dim,
        depth,
        heads,
        dim_head = 16,
        dim_out = 1,
        num_special_tokens = 2,
        attn_dropout = 0.,
        ff_dropout = 0.,
        checkpoint_grads=False,
        use_flash_attn=False
    ):
        super().__init__()
        assert all(map(lambda n: n > 0, categories)), 'number of each category must be positive'
        assert len(categories) + num_continuous > 0, 'input shape must not be null'

        # categories related calculations

        self.num_categories = len(categories)
        self.num_unique_categories = sum(categories)

        # create category embeddings table

        self.num_special_tokens = num_special_tokens
        total_tokens = self.num_unique_categories + num_special_tokens

        # for automatically offsetting unique category ids to the correct position in the categories embedding table

        if self.num_unique_categories > 0:
            categories_offset = F.pad(torch.tensor(list(categories)), (1, 0), value = num_special_tokens)
            categories_offset = categories_offset.cumsum(dim = -1)[:-1]
            self.register_buffer('categories_offset', categories_offset)

            # categorical embedding

            self.categorical_embeds = nn.Embedding(total_tokens, dim)

        # continuous

        self.num_continuous = num_continuous

        if self.num_continuous > 0:
            self.numerical_embedder = NumericalEmbedder(dim, self.num_continuous)

        # cls token

        self.cls_token = nn.Parameter(torch.randn(1, 1, dim))

        # transformer

        self.transformer = Transformer(
            dim = dim,
            depth = depth,
            heads = heads,
            dim_head = dim_head,
            attn_dropout = attn_dropout,
            ff_dropout = ff_dropout,
            checkpoint_grads=checkpoint_grads,
            use_flash_attn=use_flash_attn
        )

        # to logits

        self.to_logits = nn.Sequential(
            nn.LayerNorm(dim),
            nn.ReLU(),
            nn.Linear(dim, dim_out)
        )

    def forward(self, x_categ, x_numer, device, return_attn=False):
        x_categ = x_categ.to(device)
        x_numer = x_numer.to(device)
        
        assert x_categ.shape[-1] == self.num_categories, f'you must pass in {self.num_categories} values for your categories input'

        xs = []
        if self.num_unique_categories > 0:
            x_categ = x_categ + self.categories_offset
            x_categ = self.categorical_embeds(x_categ)
            xs.append(x_categ)

        # add numerically embedded tokens
        if self.num_continuous > 0:
            x_numer = self.numerical_embedder(x_numer)
            xs.append(x_numer)

        # concat categorical and numerical
        x = torch.cat(xs, dim=1)

        # append cls tokens
        b = x.shape[0]
        cls_tokens = repeat(self.cls_token, '1 1 d -> b 1 d', b=b)
        x = torch.cat((cls_tokens, x), dim=1)

        # attend
        if return_attn:
            x, attns = self.transformer(x, return_attn=True)
        else:
            x = self.transformer(x, return_attn=False)

        # get cls token
        x = x[:, 0]

        # out in the paper is linear(relu(ln(cls)))
        logits = self.to_logits(x)

        if not return_attn:
            return logits

        return logits, attns

    def get_embeddings(self, x_categ, x_cont, batch_size=None):
        device = next(self.parameters()).device
        x_categ = x_categ.to(device)
        x_cont = x_cont.to(device)

        if batch_size is None:
            xs = []
            if self.num_unique_categories > 0:
                x_categ = x_categ.long() + self.categories_offset  # Cast x_categ to long tensor
                x_categ = self.categorical_embeds(x_categ)
                xs.append(x_categ)

            if self.num_continuous > 0:
                x_cont = self.numerical_embedder(x_cont)
                xs.append(x_cont)

            x = torch.cat(xs, dim=1)
            b = x.shape[0]
            cls_tokens = repeat(self.cls_token, '1 1 d -> b 1 d', b=b)
            x = torch.cat((cls_tokens, x), dim=1)

            x = self.transformer(x, return_attn=False)
            embeddings = x[:, 1:]  # Exclude the CLS token from the embeddings
            return embeddings.to(device)
        else:
            embeddings = []
            for i in range(0, x_categ.size(0), batch_size):
                start = i
                end = min(start + batch_size, x_categ.size(0))

                x_categ_batch = x_categ[start:end]
                x_cont_batch = x_cont[start:end]

                xs = []
                if self.num_unique_categories > 0:
                    x_categ_batch = x_categ_batch.long() + self.categories_offset  # Cast x_categ_batch to long tensor
                    x_categ_batch = self.categorical_embeds(x_categ_batch)
                    xs.append(x_categ_batch)

                if self.num_continuous > 0:
                    x_cont_batch = self.numerical_embedder(x_cont_batch)
                    xs.append(x_cont_batch)

                x = torch.cat(xs, dim=1)
                b = x.shape[0]
                cls_tokens = repeat(self.cls_token, '1 1 d -> b 1 d', b=b)
                x = torch.cat((cls_tokens, x), dim=1)

                x = self.transformer(x, return_attn=False)
                batch_embeddings = x[:, 1:]  # Exclude the CLS token from the embeddings
                embeddings.append(batch_embeddings)

            embeddings = torch.cat(embeddings, dim=0)
            return embeddings.to(device)