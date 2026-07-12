## 概要
日頃自分の書いたコードを人に説明できるよう心がけており、自分でも説明できると自負している場合でも客観的に第三者目線から質問された時答えられない場合がある。差分をコミットする前に差分から実装や判断、基礎知識を問うことで本当にわかって実装しているのかを判定したい
「答えを直接言わず、ヒントだけを提供する学習支援AI」を実装しなさい。

## 要件
VScodeの拡張機能
LangChainを使うとgemini apiやclaudeなど裏で動くAPIモデルを制御できる

- ユーザーの質問に直接答えを与えるのではなく、適切なヒントのみを提供する
- AIのペルソナ設定はどのようなものでも構わない
- ユーザに出題または質問する機能を用意し、解答の正誤を判定できるようにすること
- Web検索などを行い、特定分野の知識ベースを構築してヒント提示に用いること
- なるべくハルシネーションを抑制し、正しい解答を判定できるよう工夫すること

1. コードを書く
2. Terminalでgit commitするとき -qオプションをつける 
3. GUIで5つの質問がされる（選択式ではなく記述式）
1,2問目は前提知識 3,4,5問目はある部分の実装が何をしているかの説明
例
---

### 第1問（前提知識）

RNN（リカレントニューラルネットワーク）が、通常の全結合ニューラルネットワークと決定的に違う点は何ですか。「再帰結合」という語を使って説明してください。

---

### 第2問（前提知識）

この実装で誤差関数として使われている**クロスエントロピー**は、何と何の間の何を測る関数ですか。

---

### 第3問（実装の説明）

次の部分は何をしているか説明してください。

```python
Z_prime = np.zeros((q, T+1))

for t in range(T):
    Z_prime[:, t+1], nabla_f[:, t] = forward(np.append(1, xi[t,:]), Z_prime[:, t], W_in, W, sigmoid)
```

---

### 第4問（実装の説明）

逆伝播のループで、なぜ `reversed`（時刻の逆順）で回す必要があるのですか。

```python
for t in reversed(range(T)):
    if t == T-1:
        delta[:, t] = backward(W, W_out[:, 1:], np.zeros(q), delta_out, nabla_f[:, t])
    else:
        delta[:, t] = backward(W, W_out[:, 1:], delta[:, t+1], np.zeros(m), nabla_f[:, t])
```

---

### 第5問（実装の説明）

`dEdW`（再帰重み $W$ の勾配）の計算で、`Z_prime` の最後の列を除いた `Z_prime[:, :T]` を使っているのはなぜですか。

```python
dEdW = np.dot(delta, Z_prime[:, :T].T)
```


以下実装差分
print("学習を行っています...")
for epoch in range(0, num_epoch):
    index = np.random.permutation(n_train)
    print("epoch =",epoch)

    e = np.full(n_train, np.nan)        
    for i in index:
        xi = x_train[i]  # xiは行列（時間幅T×チャネル数d）
        yi = y_train_vec[i]  # yiはラベルのone-hotベクトル
        T = xi.shape[0] 
        
      
        # 課題1. Z_prime, nabla_fを作成する
        Z_prime = np.zeros((q,T+1))
        nabla_f = np.zeros((q,T))

        for t in range(T):
            # Z_primeの「t+1列目」，nabla_fの「t列目」をforwardを使って求める
            Z_prime[:,t+1], nabla_f[:,t] = forward(np.append(1, xi[t,:]), Z_prime[:,t], W_in, W, sigmoid)

        Z_T = np.append(1, Z_prime[:,T])

        z_out = softmax(np.dot(W_out, Z_T))        

        ##### 誤差評価
        e[i] = CrossEntoropy(z_out, yi)

        if epoch == 0:
            # 誤差推移観察のepoch=0はパラメタ更新しない
            # (実際には最初から更新しても構わない)
            continue
        
        ##### 課題2. 逆伝播
        # delta_outを定義する（softmax + クロスエントロピーの勾配）
        delta_out = z_out - yi

        # 以下の行列の各列にdelta_1, ..., delta_Tを作成
        # backward関数の内部を作成
        delta = np.zeros((q,T)) 
        for t in reversed(range(T)):
            if t == T-1:
                delta[:,t] = backward(W, W_out[:,1:], np.zeros(q), delta_out, nabla_f[:,t]) 
            else:        
                delta[:,t] = backward(W, W_out[:,1:], delta[:,t+1], np.zeros(m), nabla_f[:,t]) 
        
        ##### 課題3. 勾配の計算

        ## dEdW_outの作成
        # ヒント: np.dotかnp.outerのどちらを使うべきか適切に判断すること
        #         また，上で作成したZ_Tを利用できる
        # delta_out(mベクトル)とZ_T(q+1ベクトル)の外積で(m x q+1)行列を作る
        dEdW_out = np.outer(delta_out, Z_T)

        ## dEdE_inの作成
        # ヒント: 以下のXが定数項含んだTx(d+1)行列
        # (np.c_は横方向の結合. Xをコンソールで見てみると
        #  何が行われいてるかわかってよい)
        X = np.hstack((np.ones(T).reshape(-1,1), xi))
        # delta(q x T)とX(T x d+1)の積で(q x d+1)行列を作る
        dEdW_in = np.dot(delta, X)

        ## dEdWの作成
        # ヒント: Z_primeの0列目からT-1列目(つまり最後の列以外)は"Z_prime[:,:T]"で指定できる
        #         また，転置の存在に注意せよ
        # delta(q x T)と\tilde{Z}'^T((T x q))の積で(q x q)行列を作る
        dEdW = np.dot(delta, Z_prime[:,:T].T)

        ##### 課題4. adamによるパラメータの更新
        n_update += 1


## あると良い機能
過去の問題と正解したかを記録し、苦手な出題傾向をルールベースでは把握して問題に反映する