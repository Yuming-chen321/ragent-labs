-- v1.1.0 261013 清理重复的 HNSW 向量索引
-- 背景：建表脚本建 idx_kv_embedding，而 PgVectorStoreAdmin 启动时只按自己的索引名 idx_kv_embedding_hnsw
--       判存，于是在同一列上又建了一个定义完全相同的 HNSW 索引：双份存储、双倍写入放大。
--       已在代码侧改为按「表 + 列 + 访问方法」判存（PgVectorStoreAdmin#ensureVectorSpace），
--       新建库不会再产生重复；本脚本用于清理已产生重复的存量库。
-- 幂等：按「表 + 列 + 访问方法」实时枚举，保留第一个索引（按名排序，通常即建表脚本的 idx_kv_embedding），
--       删除其余等价索引；无重复时执行结果为空。
-- 安全性：仅删除与保留索引定义完全等价的冗余索引，不改变任何列的语义，查询结果不受影响；
--       仅需付出一次删除开销（HNSW 索引重建成本由后续写入承担）。

DO $$
DECLARE
    keeper   text;
    dup      text;
    dup_list text[] := '{}';
BEGIN
    SELECT i.relname
    INTO keeper
    FROM pg_class t
    JOIN pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_index ix ON ix.indrelid = t.oid
    JOIN pg_class i ON i.oid = ix.indexrelid
    JOIN pg_am am ON am.oid = i.relam
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
    WHERE t.relname = 't_knowledge_vector'
      AND n.nspname = ANY (current_schemas(false))
      AND a.attname = 'embedding'
      AND am.amname = 'hnsw'
      AND ix.indisvalid
      AND ix.indnkeyatts = 1
    ORDER BY i.relname
    LIMIT 1;

    IF keeper IS NULL THEN
        RAISE NOTICE '向量列上没有任何 hnsw 索引，跳过';
        RETURN;
    END IF;

    FOR dup IN
        SELECT i.relname
        FROM pg_class t
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_index ix ON ix.indrelid = t.oid
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_am am ON am.oid = i.relam
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
        WHERE t.relname = 't_knowledge_vector'
          AND n.nspname = ANY (current_schemas(false))
          AND a.attname = 'embedding'
          AND am.amname = 'hnsw'
          AND ix.indisvalid
          AND ix.indnkeyatts = 1
          AND i.relname <> keeper
    LOOP
        EXECUTE format('DROP INDEX %I', dup);
        dup_list := dup_list || dup;
    END LOOP;

    IF array_length(dup_list, 1) IS NULL THEN
        RAISE NOTICE '向量列上无重复 hnsw 索引，保留 %', keeper;
    ELSE
        RAISE NOTICE '已保留 %，删除重复索引 %', keeper, dup_list;
    END IF;
END $$;
