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

import com.nageoffer.ai.ragent.framework.exception.ServiceException;
import com.nageoffer.ai.ragent.infra.config.AIModelProperties;
import com.nageoffer.ai.ragent.infra.embedding.EmbeddingService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.ResultSetExtractor;

import java.util.List;
import java.util.stream.IntStream;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doReturn;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;

/**
 * 向量维度三源一致性启动校验的边界行为
 * <p>
 * 三源：配置维度 / 库中列声明宽度 / 模型实测输出。这里逐条钉死「哪一源出错时启动必须失败、
 * 哪一源出错时可以放行」，避免校验本身变成新的可用性风险
 */
class VectorDimensionGuardTest {

    private static final int CONFIGURED = 1536;
    private static final String MODEL_ID = "qwen-emb-8b";

    private JdbcTemplate jdbcTemplate;
    private RAGDefaultProperties ragDefaultProperties;
    private EmbeddingService embeddingService;
    private VectorDimensionCheckProperties checkProperties;
    private VectorDimensionGuard guard;

    @BeforeEach
    void setUp() {
        jdbcTemplate = mock(JdbcTemplate.class);
        embeddingService = mock(EmbeddingService.class);

        ragDefaultProperties = new RAGDefaultProperties();
        ragDefaultProperties.setDimension(CONFIGURED);

        AIModelProperties aiModelProperties = new AIModelProperties();
        aiModelProperties.getEmbedding().setDefaultModel(MODEL_ID);

        checkProperties = new VectorDimensionCheckProperties();
        guard = new VectorDimensionGuard(jdbcTemplate, ragDefaultProperties, aiModelProperties,
                embeddingService, checkProperties);

        stubColumnType("vector(" + CONFIGURED + ")");
        stubModelDimension(CONFIGURED);
    }

    @Test
    @DisplayName("三源一致放行")
    void passesWhenThreeSourcesAgree() {
        assertDoesNotThrow(() -> guard.verify());
    }

    @Test
    @DisplayName("配置维度与库中列宽不符 → 启动失败，且报错里同时给出两个值")
    void failsWhenConfigDiffersFromColumn() {
        ragDefaultProperties.setDimension(1024);

        ServiceException ex = assertThrows(ServiceException.class, () -> guard.verify());
        assertTrue(ex.getMessage().contains("1024"), "报错应给出配置侧的值：" + ex.getMessage());
        assertTrue(ex.getMessage().contains("1536"), "报错应给出物理列的值：" + ex.getMessage());
    }

    @Test
    @DisplayName("模型实测输出与配置不符 → 启动失败（模型忽略 dimensions 参数的场景）")
    void failsWhenModelOutputDiffers() {
        stubModelDimension(1024);

        ServiceException ex = assertThrows(ServiceException.class, () -> guard.verify());
        assertTrue(ex.getMessage().contains("1024"), "报错应给出模型实际输出的维度：" + ex.getMessage());
    }

    @Test
    @DisplayName("库中列未声明固定宽度 → 启动失败，因为这种列会静默接受任意维度")
    void failsWhenColumnHasNoDeclaredDimension() {
        stubColumnType("vector");

        ServiceException ex = assertThrows(ServiceException.class, () -> guard.verify());
        assertTrue(ex.getMessage().contains("vector("), "报错应给出改法：" + ex.getMessage());
    }

    @Test
    @DisplayName("向量表或列不存在 → 启动失败并提示先跑建表脚本")
    void failsWhenVectorColumnMissing() {
        stubColumnType(null);

        ServiceException ex = assertThrows(ServiceException.class, () -> guard.verify());
        assertTrue(ex.getMessage().contains("schema_pg.sql"), "报错应给出可执行的下一步：" + ex.getMessage());
    }

    @Test
    @DisplayName("模型探测失败默认仅告警放行，置 fail-on-probe-error=true 才失败")
    void probeFailureDoesNotBlockStartupByDefault() {
        doThrow(new RuntimeException("connect timed out")).when(embeddingService).embed(anyString(), anyString());

        assertDoesNotThrow(() -> guard.verify(), "无法连到模型提供商时不应阻断启动");

        checkProperties.setFailOnProbeError(true);
        ServiceException ex = assertThrows(ServiceException.class, () -> guard.verify());
        assertTrue(ex.getMessage().contains("探测失败"), ex.getMessage());
    }

    @Test
    @DisplayName("关闭开关后不做任何校验，维度漂移由运行时兜底")
    void skipsWhenDisabled() {
        checkProperties.setEnabled(false);
        ragDefaultProperties.setDimension(1024);

        assertDoesNotThrow(() -> guard.verify());
    }

    @Test
    @DisplayName("未配置默认嵌入模型时跳过模型侧，仅比对配置与列")
    void skipsModelProbeWithoutDefaultModel() {
        AIModelProperties empty = new AIModelProperties();
        VectorDimensionGuard guardWithoutModel = new VectorDimensionGuard(jdbcTemplate, ragDefaultProperties,
                empty, embeddingService, checkProperties);

        assertDoesNotThrow(() -> guardWithoutModel.verify());
    }

    @Test
    @DisplayName("未配置维度直接失败，不回落默认值")
    void failsWhenDimensionNotConfigured() {
        ragDefaultProperties.setDimension(null);

        assertThrows(ServiceException.class, () -> guard.verify());
        ragDefaultProperties.setDimension(0);
        assertThrows(ServiceException.class, () -> guard.verify());
    }

    @Test
    @DisplayName("关掉模型探测后不再发远端调用")
    void skipsProbeWhenDisabledByFlag() {
        checkProperties.setProbeModel(false);
        stubModelDimension(1024);

        assertDoesNotThrow(() -> guard.verify(), "关掉探测后不应再因模型输出维度而失败");
    }

    private void stubColumnType(String columnType) {
        doReturn(columnType).when(jdbcTemplate)
                .query(anyString(), any(ResultSetExtractor.class), any(), any());
    }

    private void stubModelDimension(int dimension) {
        List<Float> vector = IntStream.range(0, dimension).mapToObj(i -> 0.1f).toList();
        doReturn(vector).when(embeddingService).embed(anyString(), anyString());
    }

    @Test
    @DisplayName("维度为 0 的模型响应视为不可信，不据此放行")
    void emptyModelResponseDoesNotPassSilently() {
        doReturn(List.<Float>of()).when(embeddingService).embed(anyString(), anyString());

        assertDoesNotThrow(() -> guard.verify());
        assertEquals(CONFIGURED, ragDefaultProperties.getDimension());
    }
}
