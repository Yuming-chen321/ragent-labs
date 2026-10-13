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

package com.nageoffer.ai.ragent.rag.config;

import com.nageoffer.ai.ragent.framework.errorcode.BaseErrorCode;
import com.nageoffer.ai.ragent.framework.exception.ServiceException;
import com.nageoffer.ai.ragent.infra.config.AIModelProperties;
import com.nageoffer.ai.ragent.infra.embedding.EmbeddingService;
import jakarta.annotation.PostConstruct;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 向量维度三源一致性启动校验（pg 后端）
 * <p>
 * 向量的维度同时被三处约束，任一处漂移都不会在写入时报错，而是以「检索结果莫名其妙变差」这种
 * 无异常的方式表现出来，因此这里把三处对齐检查提前到启动期：
 * <ol>
 *   <li><b>配置</b>：{@code rag.default.dimension}——向量化请求与落点判定都取自这里</li>
 *   <li><b>物理列</b>：{@code t_knowledge_vector.embedding} 的声明宽度——由建表语句决定，应用改不动</li>
 *   <li><b>模型实测输出</b>：真实调用一次默认嵌入模型量出来的向量长度——配置写对不代表模型照做，
 *       部分提供商（如本地 Ollama 的模型）会忽略请求里的 dimensions 参数按自身维度返回</li>
 * </ol>
 * 三者不一致时不静默放行：启动直接失败，把问题挡在任何一次入库之前。<b>失败策略分两档</b>——
 * 维度不一致属于配置错误，必须失败；模型探测失败（网络/鉴权）属于外部依赖抖动，默认只告警，
 * 避免把一次的远端不可达放大成整个应用起不来。
 * <p>
 * 维度取值的读取方式：{@code information_schema} 对 vector 类型只给出 USER-DEFINED，看不到维度，
 * 必须走 {@code format_type(atttypid, atttypmod)}，其返回值形如 {@code vector(1536)}
 */
@Slf4j
@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "rag.vector.type", havingValue = "pg")
public class VectorDimensionGuard {

    static final String VECTOR_TABLE = "t_knowledge_vector";
    static final String VECTOR_COLUMN = "embedding";

    /**
     * 探测文本只需能被模型接受，内容不参与判断
     */
    private static final String PROBE_TEXT = "vector dimension probe";

    private static final Pattern VECTOR_TYPE_PATTERN = Pattern.compile("^vector\\((\\d+)\\)$");

    private static final String COLUMN_TYPE_SQL = """
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = ?
              AND a.attname = ?
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND n.nspname = ANY (current_schemas(false))
            """;

    private final JdbcTemplate jdbcTemplate;
    private final RAGDefaultProperties ragDefaultProperties;
    private final AIModelProperties aiModelProperties;
    private final EmbeddingService embeddingService;
    private final VectorDimensionCheckProperties checkProperties;

    @PostConstruct
    public void verifyOnStartup() {
        verify();
    }

    /**
     * 执行三源校验；任一源不一致或不可信时抛出 {@link ServiceException} 阻断启动
     */
    void verify() {
        if (!Boolean.TRUE.equals(checkProperties.getEnabled())) {
            log.warn("向量维度一致性校验已关闭（rag.vector.dimension-check.enabled=false），"
                    + "维度漂移将退化为入库期异常或检索质量下降");
            return;
        }

        int configured = requireConfiguredDimension();
        int declared = readDeclaredColumnDimension();
        if (configured != declared) {
            throw new ServiceException(String.format(
                    "向量维度不一致：配置 rag.default.dimension=%d，但 %s.%s 的声明宽度为 %d 维。"
                            + "二者必须一致——请改配置对齐物理列，或按目标维度重建向量表（列宽无法在线修改）",
                    configured, VECTOR_TABLE, VECTOR_COLUMN, declared));
        }

        int actual = probeModelDimension();
        if (actual > 0 && actual != configured) {
            throw new ServiceException(String.format(
                    "向量维度不一致：配置 rag.default.dimension=%d，但嵌入模型 %s 实测输出 %d 维。"
                            + "模型未按请求的 dimensions 参数返回，写入时将因列宽不符而失败——"
                            + "请改用同维度模型，或把配置与向量表一并调整到模型的实际维度",
                    configured, modelId(), actual));
        }

        log.info("向量维度校验通过：配置={} / 列={} / 模型实测={}", configured, declared,
                actual > 0 ? String.valueOf(actual) : "未探测");
    }

    private int requireConfiguredDimension() {
        Integer dimension = ragDefaultProperties.getDimension();
        if (dimension == null || dimension <= 0) {
            throw new ServiceException("部署未配置向量维度 rag.default.dimension，无法校验向量空间一致性");
        }
        return dimension;
    }

    /**
     * 读取物理列的声明维度；列不存在或未声明宽度都视为不可信配置，直接失败
     */
    private int readDeclaredColumnDimension() {
        String columnType = jdbcTemplate.query(COLUMN_TYPE_SQL,
                rs -> rs.next() ? rs.getString(1) : null,
                VECTOR_TABLE, VECTOR_COLUMN);
        if (!StringUtils.hasText(columnType)) {
            throw new ServiceException(String.format(
                    "未找到向量列 %s.%s，请先执行 resources/database/schema_pg.sql 初始化表结构",
                    VECTOR_TABLE, VECTOR_COLUMN));
        }
        Matcher matcher = VECTOR_TYPE_PATTERN.matcher(columnType.trim());
        if (!matcher.matches()) {
            throw new ServiceException(String.format(
                    "%s.%s 的类型为 %s，未声明固定维度。未声明宽度的向量列会接受任意长度的向量，"
                            + "维度漂移将无法在写入时暴露——请改成 vector(<维度>)",
                    VECTOR_TABLE, VECTOR_COLUMN, columnType.trim()));
        }
        return Integer.parseInt(matcher.group(1));
    }

    /**
     * 真实调用一次默认嵌入模型，量出实际返回的向量长度
     * <p>
     * 显式指定模型而不走候选降级：降级到另一个模型会把「目标模型输出错维度」掩盖成「恰好能用」
     */
    private int probeModelDimension() {
        if (!Boolean.TRUE.equals(checkProperties.getProbeModel())) {
            log.info("已跳过嵌入模型输出维度探测（rag.vector.dimension-check.probe-model=false）");
            return -1;
        }
        String modelId = modelId();
        if (!StringUtils.hasText(modelId)) {
            log.warn("未配置默认嵌入模型 ai.embedding.default-model，跳过模型侧维度探测");
            return -1;
        }

        List<Float> vector;
        try {
            vector = embeddingService.embed(PROBE_TEXT, modelId);
        } catch (ServiceException e) {
            throw e;
        } catch (Exception e) {
            if (Boolean.TRUE.equals(checkProperties.getFailOnProbeError())) {
                throw new ServiceException("嵌入模型 " + modelId + " 维度探测失败：" + e.getMessage(),
                        e, BaseErrorCode.SERVICE_ERROR);
            }
            log.warn("嵌入模型 {} 维度探测失败，已跳过模型侧校验（不影响启动）：{}", modelId, e.getMessage());
            return -1;
        }

        if (vector == null || vector.isEmpty()) {
            log.warn("嵌入模型 {} 探测返回空向量，已跳过模型侧校验", modelId);
            return -1;
        }
        return vector.size();
    }

    private String modelId() {
        return aiModelProperties.getEmbedding().getDefaultModel();
    }
}
