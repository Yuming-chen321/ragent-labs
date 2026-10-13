/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.nageoffer.ai.ragent.rag.core.vector;

import jakarta.annotation.PostConstruct;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

import java.util.List;

@Slf4j
@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "rag.vector.type", havingValue = "pg")
public class PgVectorStoreAdmin implements VectorStoreAdmin {

    /**
     * PG 后端为全库共享一张向量表，索引也共享；索引名只是建索引当时的标识，不是判存依据
     */
    private static final String VECTOR_TABLE = "t_knowledge_vector";
    private static final String VECTOR_COLUMN = "embedding";
    private static final String DEFAULT_INDEX_NAME = "idx_kv_embedding_hnsw";

    /**
     * 已存在的等价 HNSW 索引（同表、同列、同访问方法）
     * <p>
     * 判存按「表 + 列 + 访问方法」而不是按索引名：建表脚本建的索引叫 idx_kv_embedding，
     * 本类建的叫 idx_kv_embedding_hnsw，只认自己的名字就会在已经建过索引的库上再建一个定义完全
     * 相同的索引——两份存储、双倍写入放大，且查询优化器在两者间任选，排查时极易误判
     */
    private static final String EQUIVALENT_INDEX_SQL = """
            SELECT i.relname
            FROM pg_class t
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_index ix ON ix.indrelid = t.oid
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_am am ON am.oid = i.relam
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ix.indkey[0]
            WHERE t.relname = ?
              AND n.nspname = ANY (current_schemas(false))
              AND a.attname = ?
              AND am.amname = 'hnsw'
              AND ix.indisvalid
              AND ix.indnkeyatts = 1
            ORDER BY i.relname
            """;

    private final JdbcTemplate jdbcTemplate;

    private List<String> listEquivalentHnswIndexes() {
        return jdbcTemplate.queryForList(EQUIVALENT_INDEX_SQL, String.class, VECTOR_TABLE, VECTOR_COLUMN);
    }

    /**
     * 启动自检：冗余 HNSW 索引不报错、不影响查询结果，只会安静地吃掉一份存储与一份写入开销，
     * 因此必须主动暴露。放在这里而不是 {@link #ensureVectorSpace} 里——后者只在建知识库时被调用，
     * 存量库早已建好知识库，等不到那一次调用
     */
    @PostConstruct
    public void reportRedundantHnswIndexes() {
        List<String> existing = listEquivalentHnswIndexes();
        if (existing.size() > 1) {
            log.warn("向量列上存在 {} 个等价 HNSW 索引 {}：查询结果不受影响，但会占用双份存储并放大写入开销。"
                            + "请执行 resources/database/upgrades/v1.1.0/261013_drop_duplicate_hnsw_index.sql 清理",
                    existing.size(), existing);
        }
    }

    @Override
    public void ensureVectorSpace(VectorSpaceSpec spec) {
        List<String> existing = listEquivalentHnswIndexes();

        if (existing.size() > 1) {
            log.warn("向量列上已存在 {} 个等价 HNSW 索引 {}，本次不再新建；冗余索引请执行 "
                            + "resources/database/upgrades/v1.1.0/261013_drop_duplicate_hnsw_index.sql 清理",
                    existing.size(), existing);
            return;
        }
        if (existing.size() == 1) {
            log.debug("HNSW 索引已存在: {}", existing.get(0));
            return;
        }

        log.info("创建 pgvector HNSW 索引 {}（{} 上尚无任何 hnsw 索引）", DEFAULT_INDEX_NAME, VECTOR_TABLE);
        jdbcTemplate.execute(String.format(
                "CREATE INDEX IF NOT EXISTS %s ON %s USING hnsw (%s vector_cosine_ops)",
                DEFAULT_INDEX_NAME, VECTOR_TABLE, VECTOR_COLUMN));
    }

    @Override
    public boolean vectorSpaceExists(VectorSpaceId spaceId) {
        try {
            // noinspection SqlDialectInspection,SqlNoDataSourceInspection
            jdbcTemplate.queryForObject("SELECT COUNT(*) FROM t_knowledge_vector LIMIT 1", Integer.class);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    @Override
    public void dropVectorSpace(String collectionName) {
        // PG 为共享表：仅删除该 collection 的残留向量行，不动共享 HNSW 索引
        // 常规情况下文档删除已逐一清理，此处多为 0 行的兜底
        // noinspection SqlDialectInspection,SqlNoDataSourceInspection
        int deleted = jdbcTemplate.update("DELETE FROM t_knowledge_vector WHERE collection_name = ?", collectionName);
        log.info("已删除 collection={} 的残留向量行，count={}", collectionName, deleted);
    }
}
