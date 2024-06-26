import torch
import torch.nn.functional as F
from torch import nn, einsum
from flash_attn import flash_attn_qkvpacked_func

from einops import rearrange, repeat

# helpers

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

# classes

class Residual(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(x, **kwargs) + x

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn

    def forward(self, x):
        return self.fn(self.norm(x))

# attention

class GEGLU(nn.Module):
    def forward(self, x):
        x, gates = x.chunk(2, dim = -1)
        return x * F.gelu(gates)

class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, dropout = 0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, dim),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        return self.net(x)

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

class Transformer(nn.Module):
    def __init__(
        self,
        dim,
        depth,
        heads,
        dim_head,
        attn_dropout,
        ff_dropout,
        ff_hidden_mult = 2,
        checkpoint_grads = False,
        use_flash_attn = False
    ):
        super().__init__()
        self.layers = nn.ModuleList([])

        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention(dim, heads = heads, dim_head = dim_head, dropout = attn_dropout, use_flash_attn=use_flash_attn)),
                PreNorm(dim, FeedForward(dim, dim * ff_hidden_mult, dropout = ff_dropout))
            ]))

        self.checkpoint_grads = checkpoint_grads

    def forward(self, x, return_attn=False):
        post_softmax_attns = []

        for attn, ff in self.layers:
            if return_attn:
                x, post_softmax_attn = attn(x, return_attn=True)
                post_softmax_attns.append(post_softmax_attn)
            else:
                x = x + attn(x)

            if self.checkpoint_grads:
                x = x + torch.utils.checkpoint.checkpoint(ff, x)
            else:
                x = x + ff(x)

        if not return_attn:
            return x

        return x, torch.stack(post_softmax_attns)
# mlp

class MLP(nn.Module):
    def __init__(self, dims, act = None):
        super().__init__()
        dims_pairs = list(zip(dims[:-1], dims[1:]))
        layers = []
        for ind, (dim_in, dim_out) in enumerate(dims_pairs):
            is_last = ind >= (len(dims_pairs) - 1)
            linear = nn.Linear(dim_in, dim_out)
            layers.append(linear)

            if is_last:
                continue

            act = default(act, nn.ReLU())
            layers.append(act)

        self.mlp = nn.Sequential(*layers)

    def forward(self, x):
        return self.mlp(x)

# main class

class TabTransformer(nn.Module):
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
        mlp_hidden_mults = (4, 2),
        mlp_act = None,
        num_special_tokens = 2,
        continuous_mean_std = None,
        attn_dropout = 0.,
        ff_dropout = 0.,
        use_shared_categ_embed = True,
        shared_categ_dim_divisor = 8,   # in paper, they reserve dimension / 8 for category shared embedding
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

        shared_embed_dim = 0 if not use_shared_categ_embed else int(dim // shared_categ_dim_divisor)

        self.category_embed = nn.Embedding(total_tokens, dim - shared_embed_dim)

        # take care of shared category embed

        self.use_shared_categ_embed = use_shared_categ_embed

        if use_shared_categ_embed:
            self.shared_category_embed = nn.Parameter(torch.zeros(self.num_categories, shared_embed_dim))
            nn.init.normal_(self.shared_category_embed, std = 0.02)

        # for automatically offsetting unique category ids to the correct position in the categories embedding table

        if self.num_unique_categories > 0:
            categories_offset = F.pad(torch.tensor(list(categories)), (1, 0), value = num_special_tokens)
            categories_offset = categories_offset.cumsum(dim = -1)[:-1]
            self.register_buffer('categories_offset', categories_offset)

        # continuous

        self.num_continuous = num_continuous

        if self.num_continuous > 0:
            if exists(continuous_mean_std):
                assert continuous_mean_std.shape == (num_continuous, 2), f'continuous_mean_std must have a shape of ({num_continuous}, 2) where the last dimension contains the mean and variance respectively'
            self.register_buffer('continuous_mean_std', continuous_mean_std)

            self.norm = nn.LayerNorm(num_continuous)

        # transformer

        self.transformer = Transformer(
            dim=dim,
            depth=depth,
            heads=heads,
            dim_head=dim_head,
            attn_dropout=attn_dropout,
            ff_dropout=ff_dropout,
            checkpoint_grads=checkpoint_grads,
            use_flash_attn=use_flash_attn
        )

        # mlp to logits

        input_size = (dim * self.num_categories) + num_continuous

        hidden_dimensions = [input_size * t for t in  mlp_hidden_mults]
        all_dimensions = [input_size, *hidden_dimensions, dim_out]

        self.mlp = MLP(all_dimensions, act = mlp_act)

    def forward(self, x_categ, x_cont, return_attn=False):
        xs = []

        assert x_categ.shape[-1] == self.num_categories, f'you must pass in {self.num_categories} values for your categories input'
        
        if self.num_unique_categories > 0:
            x_categ = x_categ + self.categories_offset

            categ_embed = self.category_embed(x_categ)

            if self.use_shared_categ_embed:
                shared_categ_embed = repeat(self.shared_category_embed, 'n d -> b n d', b=categ_embed.shape[0])
                categ_embed = torch.cat((categ_embed, shared_categ_embed), dim=-1)

            if return_attn:
                x, attns = self.transformer(categ_embed, return_attn=True)
            else:
                x = self.transformer(categ_embed, return_attn=False)

            flat_categ = rearrange(x, 'b ... -> b (...)')
            xs.append(flat_categ)

        assert x_cont.shape[1] == self.num_continuous, f'you must pass in {self.num_continuous} values for your continuous input'

        if self.num_continuous > 0:
            if exists(self.continuous_mean_std):
                mean, std = self.continuous_mean_std.unbind(dim = -1)
                x_cont = (x_cont - mean) / std

            normed_cont = self.norm(x_cont)
            xs.append(normed_cont)

        x = torch.cat(xs, dim = -1)
        logits = self.mlp(x)

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