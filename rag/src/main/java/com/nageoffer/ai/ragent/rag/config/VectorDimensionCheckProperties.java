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

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Configuration;

/**
 * 向量维度启动校验配置
 *
 * <pre>
 * rag:
 *   vector:
 *     dimension-check:
 *       enabled: true
 *       probe-model: true
 *       fail-on-probe-error: false
 * </pre>
 */
@Data
@Configuration
@ConfigurationProperties(prefix = "rag.vector.dimension-check")
public class VectorDimensionCheckProperties {

    /**
     * 是否开启启动期维度一致性校验，默认开启
     */
    private Boolean enabled = true;

    /**
     * 是否真实调用一次默认嵌入模型探测其输出维度，默认开启
     * <p>
     * 关掉后只比对「配置维度 / 库中列维度」，不再发远端调用，启动更快但检测不出
     * 「模型忽略 dimensions 参数、实际输出维度与配置不符」这一类问题
     */
    private Boolean probeModel = true;

    /**
     * 模型探测失败（网络不可达、鉴权失败等）时是否也判定为启动失败，默认否
     * <p>
     * 默认策略：维度不一致 → 启动失败；模型不可达 → 仅告警放行，避免把外部依赖的抖动
     * 放大成整个应用不可用。需要强一致时置为 true
     */
    private Boolean failOnProbeError = false;
}
